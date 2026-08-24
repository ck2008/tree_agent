"""Workspace persistence: a tree of projects, each holding its own conversations.

The whole workspace is a single JSON document under ~/.tree_agent.
Projects nest arbitrarily deep; conversations are always leaves.
Settings (cwd / model / sandbox) are inherited from the nearest ancestor that
defines them, so a sub-project only overrides what it actually needs.
"""

from __future__ import annotations

import json
import os
import re
import time
import uuid
from datetime import datetime
from typing import Any, Iterator

PROJECT = "project"
CONVERSATION = "conversation"

CODEX_AGENT = "codex"
CLAUDE_AGENT = "claude"
DEFAULT_AGENT = CODEX_AGENT

DEFAULT_HOME = os.path.join(os.path.expanduser("~"), ".tree_agent")

# Sandbox level for a workspace that has not been configured. Codex's own
# default is `workspace-write`, but that cannot run at all on a mapped network
# drive (see `codex_runner.sandbox_warning`), and this tool is used against
# repositories that live on one. Any project can still override it.
DEFAULT_SANDBOX = "no-sandbox"

# Where new projects start looking. Set TREE_AGENT_CWD to override; otherwise
# the first of these that exists wins, falling back to the home directory so a
# fresh machine still gets something valid.
CWD_CANDIDATES = (r"E:\GitHub\ck2008",)


def default_cwd() -> str:
    override = os.environ.get("TREE_AGENT_CWD")
    if override and os.path.isdir(override):
        return override
    for candidate in CWD_CANDIDATES:
        if os.path.isdir(candidate):
            return candidate
    return os.path.expanduser("~")


class WorkspaceLock:
    """Advisory exclusive lock on one workspace folder.

    Two windows open on the same `workspace.json` would overwrite each other's
    changes, since each holds the whole document in memory and rewrites it. The
    lock is a byte-range lock on a side file, so the OS drops it when the
    process dies — no stale lock files to clean up after a crash.

    Locking a *different* `--home` still works, which is the point: separate
    workspaces are fine, sharing one is not.
    """

    def __init__(self, home: str) -> None:
        self.path = os.path.join(home, ".lock")
        self._fh: Any = None

    # Byte 0 is the lock byte and nothing else; the owner's PID is written from
    # byte 1 on, so another process can still read it while the lock is held.
    _PID_OFFSET = 1

    def acquire(self) -> bool:
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        try:
            fh = open(self.path, "a+b")
        except OSError:
            return True  # cannot lock (odd filesystem) — do not block the user
        try:
            fh.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            fh.close()
            return False
        fh.seek(self._PID_OFFSET)
        fh.truncate(self._PID_OFFSET)
        fh.write(str(os.getpid()).encode())
        fh.flush()
        self._fh = fh
        return True

    def holder_pid(self) -> str:
        try:
            with open(self.path, "rb") as fh:
                fh.seek(self._PID_OFFSET)
                return fh.read(32).decode(errors="replace").strip() or "?"
        except OSError:
            return "?"

    def release(self) -> None:
        if self._fh is None:
            return
        try:
            if os.name == "nt":
                import msvcrt

                self._fh.seek(0)
                msvcrt.locking(self._fh.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(self._fh.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        finally:
            self._fh.close()
            self._fh = None


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def new_id() -> str:
    return uuid.uuid4().hex[:12]


_new_id = new_id  # historical name, kept for the existing call sites


def new_project(name: str) -> dict[str, Any]:
    return {
        "id": _new_id(),
        "kind": PROJECT,
        "name": name,
        "cwd": None,
        "model": None,
        "sandbox": None,
        # Free-form instructions for Codex. Unlike the settings above, these
        # accumulate down the tree instead of overriding — a client-wide rule
        # plus a subsystem-specific one is more useful than either alone.
        "prompt": None,
        "children": [],
        "expanded": True,
    }


AUTO_CONVERSATION_NAME = "新對話"
# Matches today's "新對話 2" and the older "新對話1" from before unique_name.
_AUTO_NAME = re.compile(r"^新對話\s*\d*$")
TITLE_MAX_CHARS = 26
# A token this long containing a separator is a path, not prose.
_PATH_MIN_CHARS = 14


def is_auto_name(name: str) -> bool:
    """True for the placeholder names we generate, so a rename you made is safe."""
    return bool(_AUTO_NAME.match((name or "").strip()))


def title_from(text: str) -> str:
    """A conversation title derived from its first message.

    Long paths are reduced to their file name first. Real first messages here
    look like `"F:\\a\\b\\c\\home_new.aspx" 有什麼作用` — truncating the front
    would keep the drive letter and throw away the actual question.
    """
    words = []
    for token in (text or "").split():
        bare = token.strip("\"'`<>()[]{}，。,.:：")
        if len(bare) >= _PATH_MIN_CHARS and ("\\" in bare or "/" in bare):
            tail = bare.replace("\\", "/").rstrip("/").rsplit("/", 1)[-1]
            token = tail or bare
        words.append(token)
    title = " ".join(words).strip()
    if len(title) > TITLE_MAX_CHARS:
        title = title[:TITLE_MAX_CHARS].rstrip() + "…"
    return title


def new_conversation(name: str) -> dict[str, Any]:
    return {
        "id": _new_id(),
        "kind": CONVERSATION,
        "name": name,
        "thread_id": None,
        # The selected runner is deliberately per conversation.  Existing
        # workspaces have no key, and therefore continue to mean Codex.
        "agent_id": DEFAULT_AGENT,
        "claude_session_id": None,
        # Set when this conversation branched off another one: the source's
        # Codex thread id, used once to `codex exec fork` it. Kept afterwards as
        # provenance — `thread_id` takes precedence for every later turn.
        "fork_of": None,
        "fork_of_name": None,
        "messages": [],
        "created_at": _now(),
        "updated_at": _now(),
    }


class Workspace:
    """The document model.

    Structural changes (add / rename / delete / move / settings) write
    immediately; streamed message appends only call `touch()` and are coalesced
    by the UI's `flush()` tick, so a chatty turn does not rewrite the file once
    per event. Writes go through a temp file plus rename, so a crash mid-write
    cannot corrupt the workspace.
    """

    def __init__(self, home: str = DEFAULT_HOME) -> None:
        self.home = home
        self._dirty = False
        # Counts saves that had to fall back to a non-atomic in-place write.
        self.degraded_saves = 0
        self.path = os.path.join(home, "workspace.json")
        self.data: dict[str, Any] = {
            "version": 1,
            "defaults": {
                "cwd": default_cwd(),
                "model": None,
                "sandbox": DEFAULT_SANDBOX,
            },
            "projects": [],
            "ui": {},
            "agents": {},
        }
        self.load()

    # ------------------------------------------------------------------ io

    def load(self) -> None:
        if not os.path.exists(self.path):
            self._seed()
            self.save()
            return
        loaded = self._read(self.path)
        if loaded is None:
            # An in-place save that was interrupted leaves a torn file but also
            # leaves the temp copy behind — try that before giving up on the data.
            loaded = self._read(self.path + ".tmp")
            try:
                os.replace(self.path, self.path + ".corrupt")
            except OSError:
                pass
        if loaded is None:
            self._seed()
            self.save()
            return
        self.data["defaults"].update(loaded.get("defaults") or {})
        self.data["ui"] = loaded.get("ui") or {}
        self.data["agents"] = loaded.get("agents") or {}
        self.data["projects"] = loaded.get("projects") or []
        if not self.data["projects"]:
            self._seed()

    @staticmethod
    def _read(path: str) -> dict[str, Any] | None:
        try:
            with open(path, encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, json.JSONDecodeError):
            return None
        return data if isinstance(data, dict) else None

    def _seed(self) -> None:
        root = new_project("我的專案")
        root["children"].append(new_conversation("新對話"))
        self.data["projects"] = [root]

    # Windows sharing violations on the destination are common — a virus scanner
    # or the search indexer opens the file we just wrote. Measured on this
    # machine, `os.replace` into %TEMP% fails repeatedly under load, so a short
    # retry window is not enough and giving up would lose the write.
    REPLACE_ATTEMPTS = 8
    REPLACE_BACKOFF = 0.03

    def save(self) -> None:
        """Persist the workspace, preferring an atomic rename.

        Writes to a temp file and renames it over the target. If the rename keeps
        failing with a sharing violation, the content is written straight to the
        destination instead: a millisecond-wide window where a crash could tear
        the file is far better than dropping the save. The temp file is left in
        place on that path, so `load` can fall back to it.
        """
        os.makedirs(self.home, exist_ok=True)
        tmp = self.path + ".tmp"
        payload = json.dumps(self.data, ensure_ascii=False, indent=2)
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(payload)

        for attempt in range(self.REPLACE_ATTEMPTS):
            try:
                os.replace(tmp, self.path)
                self._dirty = False
                return
            except PermissionError:
                time.sleep(self.REPLACE_BACKOFF * (attempt + 1))

        # Rename is being refused; write in place rather than lose the change.
        with open(self.path, "w", encoding="utf-8") as fh:
            fh.write(payload)
        self._dirty = False
        self.degraded_saves += 1

    def touch(self) -> None:
        """Mark the workspace dirty without writing yet (see `flush`)."""
        self._dirty = True

    def flush(self) -> bool:
        """Write only if there are pending changes. Returns True if it wrote."""
        if not self._dirty:
            return False
        self.save()
        return True

    # --------------------------------------------------------------- lookup

    @property
    def projects(self) -> list[dict[str, Any]]:
        return self.data["projects"]

    @property
    def defaults(self) -> dict[str, Any]:
        return self.data["defaults"]

    @property
    def agents(self) -> dict[str, Any]:
        """Workspace-wide agent overrides; an empty path means auto-detect."""
        return self.data.setdefault("agents", {})

    def agent_path(self, agent_id: str) -> str | None:
        value = (self.agents.get(agent_id) or {}).get("path")
        return value.strip() if isinstance(value, str) and value.strip() else None

    def set_agent_path(self, agent_id: str, path: str) -> None:
        self.agents.setdefault(agent_id, {})["path"] = path.strip() or None
        self.save()

    def conversation_agent(self, conv_id: str) -> str:
        node = self.find(conv_id)
        if node is None or node["kind"] != CONVERSATION:
            return DEFAULT_AGENT
        return node.get("agent_id") if node.get("agent_id") in (CODEX_AGENT, CLAUDE_AGENT) else DEFAULT_AGENT

    def set_conversation_agent(self, conv_id: str, agent_id: str) -> None:
        if agent_id not in (CODEX_AGENT, CLAUDE_AGENT):
            raise ValueError("未知的 Agent")
        node = self.find(conv_id)
        if node is not None and node["kind"] == CONVERSATION:
            node["agent_id"] = agent_id
            self.save()

    def walk(self) -> Iterator[tuple[dict[str, Any], dict[str, Any] | None]]:
        """Yield (node, parent) depth-first; parent is None for top-level."""

        def rec(nodes, parent):
            for node in nodes:
                yield node, parent
                if node["kind"] == PROJECT:
                    yield from rec(node["children"], node)

        yield from rec(self.projects, None)

    def find(self, node_id: str | None) -> dict[str, Any] | None:
        if not node_id:
            return None
        for node, _ in self.walk():
            if node["id"] == node_id:
                return node
        return None

    def parent_of(self, node_id: str) -> dict[str, Any] | None:
        for node, parent in self.walk():
            if node["id"] == node_id:
                return parent
        return None

    def siblings_of(self, node_id: str) -> list[dict[str, Any]]:
        parent = self.parent_of(node_id)
        return parent["children"] if parent else self.projects

    def ancestors(self, node_id: str) -> list[dict[str, Any]]:
        """Nearest-first chain of ancestor projects."""
        chain: list[dict[str, Any]] = []
        cur = node_id
        while True:
            parent = self.parent_of(cur)
            if parent is None:
                return chain
            chain.append(parent)
            cur = parent["id"]

    def path_of(self, node_id: str) -> str:
        node = self.find(node_id)
        if node is None:
            return ""
        names = [a["name"] for a in reversed(self.ancestors(node_id))]
        names.append(node["name"])
        return " / ".join(names)

    def owning_project(self, node_id: str) -> dict[str, Any] | None:
        """The project a new child should go under, given the selection."""
        node = self.find(node_id)
        if node is None:
            return None
        if node["kind"] == PROJECT:
            return node
        return self.parent_of(node_id)

    # ------------------------------------------------------------- settings

    def resolve(self, node_id: str) -> dict[str, Any]:
        """Effective cwd / model / sandbox for a node, walking up the tree."""
        node = self.find(node_id)
        chain = ([node] if node else []) + self.ancestors(node_id)
        out = dict(self.defaults)
        for key in ("cwd", "model", "sandbox"):
            for candidate in chain:
                value = candidate.get(key)
                if value:
                    out[key] = value
                    break
        return out

    def instructions_for(self, node_id: str, include_self: bool = True) -> str:
        """Project instructions from the root down to `node_id`, joined.

        Concatenated rather than overridden, so a broad rule set on an outer
        project still applies inside its sub-projects.
        """
        chain = list(reversed(self.ancestors(node_id)))
        if include_self:
            node = self.find(node_id)
            if node is not None:
                chain.append(node)
        parts = [n.get("prompt", "").strip() for n in chain if (n.get("prompt") or "").strip()]
        return "\n\n".join(parts)

    def inherited(self, node_id: str, key: str) -> Any:
        """What `node_id` would use for `key` if it defined nothing itself."""
        for candidate in self.ancestors(node_id):
            if candidate.get(key):
                return candidate[key]
        return self.defaults.get(key)

    # ------------------------------------------------------------ mutations

    def add_project(self, parent_id: str | None, name: str) -> dict[str, Any]:
        node = new_project(name)
        if parent_id is None:
            self.projects.append(node)
        else:
            parent = self.find(parent_id)
            if parent is None or parent["kind"] != PROJECT:
                raise ValueError("只能在專案底下新增子專案")
            parent["children"].append(node)
            parent["expanded"] = True
        self.save()
        return node

    def add_conversation(self, project_id: str, name: str) -> dict[str, Any]:
        project = self.find(project_id)
        if project is None or project["kind"] != PROJECT:
            raise ValueError("對話必須屬於某個專案")
        node = new_conversation(name)
        project["children"].append(node)
        project["expanded"] = True
        self.save()
        return node

    def unique_name(self, parent: dict[str, Any], base: str) -> str:
        """`base`, or "base 2", "base 3"… so siblings never share a name."""
        taken = {c["name"] for c in parent["children"]}
        if base not in taken:
            return base
        counter = 2
        while f"{base} {counter}" in taken:
            counter += 1
        return f"{base} {counter}"

    _FORK_SUFFIX = re.compile(r"\s*\(分岔(?:\s*\d+)?\)$")

    def fork_name(self, parent: dict[str, Any], base: str) -> str:
        """A free "<base> (分岔)" name, without stacking suffixes on re-forks."""
        base = self._FORK_SUFFIX.sub("", base)
        taken = {c["name"] for c in parent["children"]}
        candidate = f"{base} (分岔)"
        counter = 2
        while candidate in taken:
            candidate = f"{base} (分岔 {counter})"
            counter += 1
        return candidate

    def fork_conversation(self, conv_id: str) -> dict[str, Any] | None:
        """Branch a conversation: a sibling that shares the history so far.

        The transcript is copied so the new conversation shows what the model
        already knows, and `fork_of` records the source thread so the first turn
        can `codex exec fork` it. Returns None when there is nothing to fork
        (the source has never run, so it has no thread yet).
        """
        conv = self.find(conv_id)
        parent = self.parent_of(conv_id)
        if conv is None or conv["kind"] != CONVERSATION or parent is None:
            return None
        if not conv.get("thread_id"):
            return None

        node = new_conversation(self.fork_name(parent, conv["name"]))
        node["fork_of"] = conv["thread_id"]
        node["fork_of_name"] = conv["name"]
        node["messages"] = [dict(m) for m in conv["messages"]]
        node["messages"].append(
            {
                "role": "meta",
                "text": f"以上內容分岔自「{conv['name']}」。從這裡開始，兩邊各走各的。",
                "ts": _now(),
            }
        )
        # Sit just below the source, after any sibling forks of the same thread,
        # so repeated forks read in the order they were made.
        insert_at = next(
            i for i, n in enumerate(parent["children"]) if n["id"] == conv_id
        ) + 1
        while (
            insert_at < len(parent["children"])
            and parent["children"][insert_at].get("fork_of") == conv["thread_id"]
        ):
            insert_at += 1
        parent["children"].insert(insert_at, node)
        self.save()
        return node

    def rename(self, node_id: str, name: str) -> None:
        node = self.find(node_id)
        if node is not None:
            node["name"] = name
            self.save()

    def delete(self, node_id: str) -> None:
        siblings = self.siblings_of(node_id)
        for i, node in enumerate(siblings):
            if node["id"] == node_id:
                del siblings[i]
                break
        self.save()

    def move(self, node_id: str, new_parent_id: str | None, index: int | None = None) -> bool:
        """Reparent a node. Returns False when the move is not legal."""
        node = self.find(node_id)
        if node is None or new_parent_id == node_id:
            return False

        if new_parent_id is None:
            if node["kind"] == CONVERSATION:
                return False  # conversations always live inside a project
            target = self.projects
        else:
            new_parent = self.find(new_parent_id)
            if new_parent is None or new_parent["kind"] != PROJECT:
                return False
            # Refuse to drop a project into its own subtree.
            if any(a["id"] == node_id for a in self.ancestors(new_parent_id)):
                return False
            target = new_parent["children"]
            new_parent["expanded"] = True

        siblings = self.siblings_of(node_id)
        old_index = next(i for i, n in enumerate(siblings) if n["id"] == node_id)
        del siblings[old_index]
        if index is None:
            target.append(node)
        else:
            if siblings is target and index > old_index:
                index -= 1
            target.insert(max(0, min(index, len(target))), node)
        self.save()
        return True

    def set_option(self, node_id: str, key: str, value: Any) -> None:
        node = self.find(node_id)
        if node is not None:
            node[key] = value or None
            self.save()

    def set_expanded(self, node_id: str, expanded: bool) -> None:
        node = self.find(node_id)
        if node is not None and node["kind"] == PROJECT:
            node["expanded"] = expanded

    # ------------------------------------------------------------- messages

    def append_message(self, conv_id: str, role: str, text: str, **extra: Any) -> dict[str, Any] | None:
        conv = self.find(conv_id)
        if conv is None or conv["kind"] != CONVERSATION:
            return None
        msg: dict[str, Any] = {"role": role, "text": text, "ts": _now()}
        msg.update(extra)
        conv["messages"].append(msg)
        conv["updated_at"] = msg["ts"]
        self.touch()
        return msg

    USAGE_KEYS = ("input_tokens", "cached_input_tokens", "output_tokens",
                  "reasoning_output_tokens")

    def add_usage(self, conv_id: str, usage: dict[str, Any]) -> dict[str, int]:
        """Fold one turn's token counts into the conversation's running total."""
        conv = self.find(conv_id)
        if conv is None or conv["kind"] != CONVERSATION:
            return {}
        total = conv.setdefault("usage", {})
        total["turns"] = total.get("turns", 0) + 1
        for key in self.USAGE_KEYS:
            value = usage.get(key)
            if isinstance(value, int):
                total[key] = total.get(key, 0) + value
        self.touch()
        return total

    def usage_of(self, node_id: str) -> dict[str, int]:
        """Totals for a conversation, or the sum over a project's whole subtree."""
        node = self.find(node_id)
        if node is None:
            return {}
        if node["kind"] == CONVERSATION:
            return dict(node.get("usage") or {})

        total: dict[str, int] = {}
        stack = [node]
        while stack:
            current = stack.pop()
            if current["kind"] == PROJECT:
                stack.extend(current["children"])
                continue
            for key, value in (current.get("usage") or {}).items():
                if isinstance(value, int):
                    total[key] = total.get(key, 0) + value
        return total

    def set_thread_id(self, conv_id: str, thread_id: str) -> None:
        conv = self.find(conv_id)
        if conv is not None and conv.get("thread_id") != thread_id:
            conv["thread_id"] = thread_id
            self.save()

    def clear_thread(self, conv_id: str) -> None:
        """Forget every runner session and wipe the transcript, keeping the node."""
        conv = self.find(conv_id)
        if conv is not None:
            conv["thread_id"] = None
            conv["claude_session_id"] = None
            conv["fork_of"] = None
            conv["fork_of_name"] = None
            conv["messages"] = []
            self.save()
