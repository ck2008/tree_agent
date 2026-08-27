"""Projects and conversations: the rules that used to live in `store.py`.

Everything `Workspace` enforced in memory is enforced here instead, inside the
same transaction as the write: sibling names stay unique, conversations stay
leaves, a project cannot be moved into its own subtree, settings inherit from
the nearest ancestor that defines them, and project prompts accumulate from the
root down rather than overriding.

Two extras the JSON document never needed: every structural change checks the
caller's permission, and every user-visible edit carries a `revision` so two
people editing the same node get a predictable 409 instead of last-write-wins.
"""

from __future__ import annotations

import re
import sqlite3
from typing import Any

from .. import ids
from ..db import Database
from ..errors import ConflictError, NameConflict, NotFound, RevisionConflict, ValidationError
from ..repositories import conversations as conversations_repo
from ..repositories import messages as messages_repo
from ..repositories import projects as projects_repo
from ..repositories import search as search_repo
from ..repositories import users as users_repo
from .access import Actor, require_admin, require_owner, require_read, require_root_write, require_write
from .access import visible_projects

# Settings resolve upwards, one key at a time; prompts concatenate downwards.
INHERITED_KEYS = ("cwd", "model", "sandbox", "claude_permission")

AUTO_CONVERSATION_NAME = "新對話"
_FORK_SUFFIX = re.compile(r"\s*\(分岔(?:\s*\d+)?\)$")
NAME_MAX = 200


def _clean_name(name: str) -> str:
    name = (name or "").strip()
    if not 1 <= len(name) <= NAME_MAX:
        raise ValidationError(f"名稱長度需介於 1 到 {NAME_MAX} 個字元")
    return name


def place(siblings: list[tuple[str, str]], index: int | None) -> tuple[str, list[tuple[str, str]]]:
    """A sort key for position `index`, plus any resequencing it forced.

    `siblings` is the level's current (id, sort_key) in order. When the gap has
    been split too many times to hold another key, the whole level is
    renumbered — in the caller's transaction, so readers never see it half done.
    """
    keys = [key for _, key in siblings]
    if index is None or index > len(keys):
        index = len(keys)
    index = max(0, index)
    before = keys[index - 1] if index > 0 else None
    after = keys[index] if index < len(keys) else None
    try:
        return ids.rank_between(before, after), []
    except ids.RankExhausted:
        fresh = ids.resequence(len(siblings) + 1)
        updates = [
            (node_id, fresh[position if position < index else position + 1])
            for position, (node_id, _) in enumerate(siblings)
        ]
        return fresh[index], updates


class TreeService:
    def __init__(self, db: Database, defaults: dict[str, Any] | None = None) -> None:
        self.db = db
        self.defaults = dict(defaults or {})

    # ------------------------------------------------------------- reading

    def tree(self, actor: Actor) -> dict[str, Any]:
        """The forest of everything the caller can see, conversations included.

        A project whose parent is invisible is surfaced at the top level: the
        grant is on the child, and hiding it because of its parent would make
        the project unreachable.
        """
        with self.db.read() as conn:
            visible = visible_projects(conn, actor)
            rows = {row["id"]: row for row in projects_repo.list_live(conn) if row["id"] in visible}
            nodes: dict[str, dict[str, Any]] = {}
            for project_id, row in rows.items():
                node = projects_repo.row_to_project(row)
                node["permission"] = visible[project_id]
                node["kind"] = "project"
                node["children"] = []
                node["conversations"] = []
                nodes[project_id] = node

            for row in conversations_repo.list_live(conn):
                parent = nodes.get(row["project_id"])
                if parent is not None:
                    conversation = conversations_repo.row_to_conversation(row)
                    conversation["kind"] = "conversation"
                    parent["conversations"].append(conversation)

            roots: list[dict[str, Any]] = []
            for project_id, node in nodes.items():
                parent_id = node["parent_id"]
                if parent_id in nodes:
                    nodes[parent_id]["children"].append(node)
                else:
                    roots.append(node)
            _sort_tree(roots)
        return {"projects": roots, "defaults": dict(self.defaults)}

    def get_project(self, actor: Actor, project_id: str) -> dict[str, Any]:
        with self.db.read() as conn:
            permission = require_read(conn, actor, project_id)
            project = projects_repo.row_to_project(projects_repo.get(conn, project_id))
            if project is None:
                raise NotFound("找不到專案")
            project["permission"] = permission
            project["path"] = self._path_of(conn, project_id)
            return project

    def get_conversation(self, actor: Actor, conversation_id: str) -> dict[str, Any]:
        with self.db.read() as conn:
            row = self._conversation_or_404(conn, actor, conversation_id)
            conversation = conversations_repo.row_to_conversation(row)
            conversation["path"] = self._path_of(conn, row["project_id"], conversation["name"])
            conversation["permission"] = require_read(conn, actor, row["project_id"])
            return conversation

    def resolve(self, actor: Actor, node_id: str) -> dict[str, Any]:
        """Effective execution settings for a project or conversation."""
        with self.db.read() as conn:
            project_id, conversation = self._locate(conn, actor, node_id)
            chain = projects_repo.ancestors(conn, project_id)
            resolved = dict(self.defaults)
            for key in INHERITED_KEYS:
                if conversation is not None and key == "model" and conversation["model"]:
                    resolved[key] = conversation["model"]
                    continue
                for row in chain:
                    if row[key]:
                        resolved[key] = row[key]
                        break
            return resolved

    def instructions(self, actor: Actor, node_id: str) -> str:
        """Project prompts from the root down, joined — never overridden."""
        with self.db.read() as conn:
            project_id, _ = self._locate(conn, actor, node_id)
            chain = list(reversed(projects_repo.ancestors(conn, project_id)))
            parts = [(row["prompt"] or "").strip() for row in chain]
            return "\n\n".join(part for part in parts if part)

    def path_of(self, actor: Actor, node_id: str) -> str:
        with self.db.read() as conn:
            project_id, conversation = self._locate(conn, actor, node_id)
            leaf = conversation["name"] if conversation is not None else None
            return self._path_of(conn, project_id, leaf)

    # ------------------------------------------------------------ projects

    def create_project(
        self,
        actor: Actor,
        *,
        parent_id: str | None,
        name: str,
        index: int | None = None,
        settings: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        name = _clean_name(name)
        settings = {
            key: value for key, value in (settings or {}).items() if key in projects_repo.SETTING_KEYS
        }

        def job(conn: sqlite3.Connection) -> dict[str, Any]:
            if parent_id is None:
                require_root_write(actor)
            else:
                require_write(conn, actor, parent_id)
            if projects_repo.live_name_taken(conn, parent_id, name):
                raise NameConflict(f"同一層已經有「{name}」了")
            sort_key, moves = place(_project_siblings(conn, parent_id), index)
            _apply_sort_updates(conn, projects_repo, moves)
            now = ids.now_ms()
            project_id = projects_repo.insert(
                conn,
                parent_id=parent_id,
                name=name,
                sort_key=sort_key,
                created_by=actor.id,
                now_ms=now,
                settings=settings,
            )
            # The creator owns what they created; without this a member could
            # make a project inside a shared parent and then not manage it.
            projects_repo.grant(
                conn,
                project_id=project_id,
                user_id=actor.id,
                permission="owner",
                granted_by=actor.id,
                now_ms=now,
            )
            if parent_id is not None:
                projects_repo.set_expanded(conn, parent_id, True)
            search_repo.index_project(conn, project_id, name, settings.get("prompt"))
            return projects_repo.row_to_project(projects_repo.get(conn, project_id))

        return self.db.write(job, label="create_project")

    def update_project(
        self, actor: Actor, project_id: str, *, revision: int | None, fields: dict[str, Any]
    ) -> dict[str, Any]:
        allowed = {"name", *projects_repo.SETTING_KEYS}
        unknown = set(fields) - allowed
        if unknown:
            raise ValidationError(f"不能更新這些欄位：{', '.join(sorted(unknown))}")
        if "name" in fields:
            fields = dict(fields, name=_clean_name(fields["name"]))

        def job(conn: sqlite3.Connection) -> dict[str, Any]:
            require_write(conn, actor, project_id)
            row = projects_repo.get(conn, project_id)
            if row is None:
                raise NotFound("找不到專案")
            if "name" in fields and fields["name"].lower() != row["name"].lower():
                if projects_repo.live_name_taken(conn, row["parent_id"], fields["name"]):
                    raise NameConflict(f"同一層已經有「{fields['name']}」了")
            changed = projects_repo.update(
                conn, project_id, fields, expected_revision=revision, now_ms=ids.now_ms()
            )
            if changed == 0:
                _revision_conflict(row)
            updated = projects_repo.get(conn, project_id)
            search_repo.index_project(conn, project_id, updated["name"], updated["prompt"])
            return projects_repo.row_to_project(updated)

        return self.db.write(job, label="update_project")

    def set_expanded(self, actor: Actor, project_id: str, expanded: bool) -> None:
        def job(conn: sqlite3.Connection) -> None:
            require_read(conn, actor, project_id)
            projects_repo.set_expanded(conn, project_id, expanded)

        self.db.write(job, label="set_expanded")

    def move_project(
        self,
        actor: Actor,
        project_id: str,
        *,
        revision: int | None,
        parent_id: str | None,
        index: int | None = None,
    ) -> dict[str, Any]:
        def job(conn: sqlite3.Connection) -> dict[str, Any]:
            row = projects_repo.get(conn, project_id)
            if row is None:
                require_read(conn, actor, project_id)  # raises 404 consistently
                raise NotFound("找不到專案")
            require_write(conn, actor, project_id)
            if parent_id is None:
                require_root_write(actor)
            else:
                require_write(conn, actor, parent_id)
                target = projects_repo.get(conn, parent_id)
                if target is None:
                    raise NotFound("找不到目的專案")
                if parent_id == project_id:
                    raise ValidationError("不能把專案移到自己底下")
                # `descendant_ids` includes the project itself, which is exactly
                # the cycle we have to refuse along with every node under it.
                if parent_id in projects_repo.descendant_ids(conn, project_id, live_only=False):
                    raise ValidationError("不能把專案移到自己的子專案底下")
            if projects_repo.live_name_taken(conn, parent_id, row["name"], exclude_id=project_id):
                raise NameConflict(f"目的地已經有「{row['name']}」了")

            siblings = [
                item for item in _project_siblings(conn, parent_id) if item[0] != project_id
            ]
            sort_key, moves = place(siblings, index)
            _apply_sort_updates(conn, projects_repo, moves)
            changed = projects_repo.update(
                conn,
                project_id,
                {"parent_id": parent_id, "sort_key": sort_key},
                expected_revision=revision,
                now_ms=ids.now_ms(),
            )
            if changed == 0:
                _revision_conflict(row)
            if parent_id is not None:
                projects_repo.set_expanded(conn, parent_id, True)
            return projects_repo.row_to_project(projects_repo.get(conn, project_id))

        return self.db.write(job, label="move_project")

    def delete_project(self, actor: Actor, project_id: str, *, revision: int) -> dict[str, Any]:
        def job(conn: sqlite3.Connection) -> dict[str, Any]:
            require_write(conn, actor, project_id)
            row = projects_repo.get(conn, project_id)
            if row is None:
                raise NotFound("找不到專案")
            if row["revision"] != revision:
                _revision_conflict(row)
            now = ids.now_ms()
            touched = projects_repo.soft_delete_subtree(
                conn, project_id, deleted_by=actor.id, now_ms=now
            )
            search_repo.drop_projects(conn, touched["projects"])
            search_repo.drop_conversations(conn, touched["conversations"])
            search_repo.drop_messages(conn, touched["messages"])
            return {"deleted_at_ms": now, **{k: len(v) for k, v in touched.items()}}

        return self.db.write(job, label="delete_project")

    def restore_project(self, actor: Actor, project_id: str) -> dict[str, Any]:
        def job(conn: sqlite3.Connection) -> dict[str, Any]:
            row = projects_repo.get(conn, project_id, include_deleted=True)
            if row is None or row["deleted_at_ms"] is None:
                raise NotFound("找不到已刪除的專案")
            parent_id = row["parent_id"]
            if parent_id is None:
                require_admin(actor)
            else:
                parent = projects_repo.get(conn, parent_id)
                if parent is None:
                    raise ConflictError("原本的上層專案已不存在，請先復原它")
                require_write(conn, actor, parent_id)
            if projects_repo.live_name_taken(conn, parent_id, row["name"], exclude_id=project_id):
                raise NameConflict(f"同一層已經有「{row['name']}」了，請先改名")

            touched = projects_repo.restore_subtree(conn, project_id, row["deleted_at_ms"])
            self._reindex(conn, touched)
            return {k: len(v) for k, v in touched.items()}

        return self.db.write(job, label="restore_project")

    # --------------------------------------------------------- memberships

    def memberships(self, actor: Actor, project_id: str) -> list[dict[str, Any]]:
        with self.db.read() as conn:
            require_read(conn, actor, project_id)
            return projects_repo.memberships_for_project(conn, project_id)

    def grant(
        self, actor: Actor, project_id: str, *, user_id: str, permission: str
    ) -> list[dict[str, Any]]:
        if permission not in projects_repo.PERMISSIONS:
            raise ValidationError(f"未知的權限：{permission}")

        def job(conn: sqlite3.Connection) -> list[dict[str, Any]]:
            require_owner(conn, actor, project_id)
            if users_repo.get(conn, user_id) is None:
                raise NotFound("找不到使用者")
            projects_repo.grant(
                conn,
                project_id=project_id,
                user_id=user_id,
                permission=permission,
                granted_by=actor.id,
                now_ms=ids.now_ms(),
            )
            return projects_repo.memberships_for_project(conn, project_id)

        return self.db.write(job, label="grant_membership")

    def revoke(self, actor: Actor, project_id: str, *, user_id: str) -> list[dict[str, Any]]:
        def job(conn: sqlite3.Connection) -> list[dict[str, Any]]:
            require_owner(conn, actor, project_id)
            projects_repo.revoke(conn, project_id, user_id)
            return projects_repo.memberships_for_project(conn, project_id)

        return self.db.write(job, label="revoke_membership")

    # ------------------------------------------------------- conversations

    def list_conversations(self, actor: Actor, project_id: str) -> list[dict[str, Any]]:
        with self.db.read() as conn:
            require_read(conn, actor, project_id)
            return [
                conversations_repo.row_to_conversation(row)
                for row in conversations_repo.list_for_project(conn, project_id)
            ]

    def create_conversation(
        self,
        actor: Actor,
        *,
        project_id: str,
        name: str | None = None,
        agent_id: str = conversations_repo.DEFAULT_AGENT,
        model: str | None = None,
        index: int | None = None,
    ) -> dict[str, Any]:
        if agent_id not in conversations_repo.AGENTS:
            raise ValidationError(f"未知的 Agent：{agent_id}")

        def job(conn: sqlite3.Connection) -> dict[str, Any]:
            require_write(conn, actor, project_id)
            if projects_repo.get(conn, project_id) is None:
                raise NotFound("找不到專案")
            wanted = _clean_name(name) if name else AUTO_CONVERSATION_NAME
            unique = self._unique_conversation_name(conn, project_id, wanted, explicit=bool(name))
            sort_key, moves = place(_conversation_siblings(conn, project_id), index)
            _apply_sort_updates(conn, conversations_repo, moves)
            now = ids.now_ms()
            conversation_id = conversations_repo.insert(
                conn,
                project_id=project_id,
                name=unique,
                sort_key=sort_key,
                agent_id=agent_id,
                model=model,
                created_by=actor.id,
                now_ms=now,
            )
            projects_repo.set_expanded(conn, project_id, True)
            search_repo.index_conversation(conn, conversation_id, project_id, unique)
            return conversations_repo.row_to_conversation(
                conversations_repo.get(conn, conversation_id)
            )

        return self.db.write(job, label="create_conversation")

    def update_conversation(
        self,
        actor: Actor,
        conversation_id: str,
        *,
        revision: int | None,
        fields: dict[str, Any],
    ) -> dict[str, Any]:
        allowed = {"name", "agent_id", "model"}
        unknown = set(fields) - allowed
        if unknown:
            raise ValidationError(f"不能更新這些欄位：{', '.join(sorted(unknown))}")
        if "agent_id" in fields and fields["agent_id"] not in conversations_repo.AGENTS:
            raise ValidationError(f"未知的 Agent：{fields['agent_id']}")
        if "name" in fields:
            fields = dict(fields, name=_clean_name(fields["name"]))

        def job(conn: sqlite3.Connection) -> dict[str, Any]:
            row = self._conversation_or_404(conn, actor, conversation_id)
            require_write(conn, actor, row["project_id"])
            if "name" in fields and fields["name"].lower() != row["name"].lower():
                if conversations_repo.live_name_taken(
                    conn, row["project_id"], fields["name"], exclude_id=conversation_id
                ):
                    raise NameConflict(f"這個專案裡已經有「{fields['name']}」了")
            changed = conversations_repo.update(
                conn, conversation_id, fields, expected_revision=revision, now_ms=ids.now_ms()
            )
            if changed == 0:
                _revision_conflict(row)
            updated = conversations_repo.get(conn, conversation_id)
            if "name" in fields:
                search_repo.index_conversation(
                    conn, conversation_id, updated["project_id"], updated["name"]
                )
            return conversations_repo.row_to_conversation(updated)

        return self.db.write(job, label="update_conversation")

    def set_runner_state(
        self,
        actor: Actor,
        conversation_id: str,
        *,
        codex_thread_id: str | None = None,
        claude_session_id: str | None = None,
    ) -> dict[str, Any]:
        """Record ids the runner handed back.

        Deliberately outside the revision check: these are facts about a session
        that already exists, not an edit two people can disagree about.
        """
        fields: dict[str, Any] = {}
        if codex_thread_id is not None:
            fields["codex_thread_id"] = codex_thread_id
        if claude_session_id is not None:
            fields["claude_session_id"] = claude_session_id
        if not fields:
            raise ValidationError("沒有要更新的欄位")

        def job(conn: sqlite3.Connection) -> dict[str, Any]:
            row = self._conversation_or_404(conn, actor, conversation_id)
            require_write(conn, actor, row["project_id"])
            conversations_repo.update(
                conn, conversation_id, fields, expected_revision=None, now_ms=ids.now_ms()
            )
            return conversations_repo.row_to_conversation(
                conversations_repo.get(conn, conversation_id)
            )

        return self.db.write(job, label="set_runner_state")

    def move_conversation(
        self,
        actor: Actor,
        conversation_id: str,
        *,
        revision: int | None,
        project_id: str,
        index: int | None = None,
    ) -> dict[str, Any]:
        def job(conn: sqlite3.Connection) -> dict[str, Any]:
            row = self._conversation_or_404(conn, actor, conversation_id)
            require_write(conn, actor, row["project_id"])
            require_write(conn, actor, project_id)
            if projects_repo.get(conn, project_id) is None:
                raise NotFound("找不到目的專案")
            if conversations_repo.live_name_taken(
                conn, project_id, row["name"], exclude_id=conversation_id
            ):
                raise NameConflict(f"目的專案已經有「{row['name']}」了")
            siblings = [
                item
                for item in _conversation_siblings(conn, project_id)
                if item[0] != conversation_id
            ]
            sort_key, moves = place(siblings, index)
            _apply_sort_updates(conn, conversations_repo, moves)
            changed = conversations_repo.update(
                conn,
                conversation_id,
                {"project_id": project_id, "sort_key": sort_key},
                expected_revision=revision,
                now_ms=ids.now_ms(),
            )
            if changed == 0:
                _revision_conflict(row)
            # The conversation and every message under it now belong to another
            # project, and search filters on that id.
            search_repo.reindex_conversation(conn, conversation_id, project_id, row["name"])
            projects_repo.set_expanded(conn, project_id, True)
            return conversations_repo.row_to_conversation(
                conversations_repo.get(conn, conversation_id)
            )

        return self.db.write(job, label="move_conversation")

    def fork_conversation(self, actor: Actor, conversation_id: str) -> dict[str, Any]:
        """A sibling that starts with the same transcript and then diverges."""

        def job(conn: sqlite3.Connection) -> dict[str, Any]:
            source = self._conversation_or_404(conn, actor, conversation_id)
            project_id = source["project_id"]
            require_write(conn, actor, project_id)

            name = self._fork_name(conn, project_id, source["name"])
            siblings = _conversation_siblings(conn, project_id)
            index = _fork_position(conn, siblings, conversation_id, source["codex_thread_id"])
            sort_key, moves = place(siblings, index)
            _apply_sort_updates(conn, conversations_repo, moves)

            now = ids.now_ms()
            new_id = conversations_repo.insert(
                conn,
                project_id=project_id,
                name=name,
                sort_key=sort_key,
                agent_id=source["agent_id"],
                model=source["model"],
                created_by=actor.id,
                now_ms=now,
                forked_from_conversation_id=conversation_id,
                forked_from_external_session_id=source["codex_thread_id"]
                or source["claude_session_id"],
            )
            copied = self._copy_transcript(conn, conversation_id, new_id, now)
            messages_repo.insert(
                conn,
                conversation_id=new_id,
                role="meta",
                content=f"以上內容分岔自「{source['name']}」。從這裡開始，兩邊各走各的。",
                now_ms=now,
                created_by=actor.id,
            )
            search_repo.reindex_conversation(conn, new_id, project_id, name)
            result = conversations_repo.row_to_conversation(conversations_repo.get(conn, new_id))
            result["copied_messages"] = copied
            return result

        return self.db.write(job, label="fork_conversation")

    def delete_conversation(
        self, actor: Actor, conversation_id: str, *, revision: int
    ) -> dict[str, Any]:
        def job(conn: sqlite3.Connection) -> dict[str, Any]:
            row = self._conversation_or_404(conn, actor, conversation_id)
            require_write(conn, actor, row["project_id"])
            if row["revision"] != revision:
                _revision_conflict(row)
            now = ids.now_ms()
            message_ids = conversations_repo.soft_delete(
                conn, conversation_id, deleted_by=actor.id, now_ms=now
            )
            search_repo.drop_conversations(conn, [conversation_id])
            search_repo.drop_messages(conn, message_ids)
            return {"deleted_at_ms": now, "messages": len(message_ids)}

        return self.db.write(job, label="delete_conversation")

    def restore_conversation(self, actor: Actor, conversation_id: str) -> dict[str, Any]:
        def job(conn: sqlite3.Connection) -> dict[str, Any]:
            row = conversations_repo.get(conn, conversation_id, include_deleted=True)
            if row is None or row["deleted_at_ms"] is None:
                raise NotFound("找不到已刪除的對話")
            project = projects_repo.get(conn, row["project_id"])
            if project is None:
                raise ConflictError("原本的專案已不存在，請先復原它")
            require_write(conn, actor, row["project_id"])
            if conversations_repo.live_name_taken(
                conn, row["project_id"], row["name"], exclude_id=conversation_id
            ):
                raise NameConflict(f"這個專案裡已經有「{row['name']}」了，請先改名")
            conversations_repo.restore(conn, conversation_id, row["deleted_at_ms"])
            search_repo.reindex_conversation(
                conn, conversation_id, row["project_id"], row["name"]
            )
            return conversations_repo.row_to_conversation(
                conversations_repo.get(conn, conversation_id)
            )

        return self.db.write(job, label="restore_conversation")

    def reset_conversation(self, actor: Actor, conversation_id: str) -> dict[str, Any]:
        """Forget every runner session and clear the transcript, keeping the node.

        The messages are soft-deleted like everything else, so a reset started
        by mistake is still recoverable by an admin.
        """

        def job(conn: sqlite3.Connection) -> dict[str, Any]:
            row = self._conversation_or_404(conn, actor, conversation_id)
            require_write(conn, actor, row["project_id"])
            now = ids.now_ms()
            message_ids = messages_repo.live_ids(conn, conversation_id)
            for message_id in message_ids:
                messages_repo.soft_delete(conn, message_id, now)
            search_repo.drop_messages(conn, message_ids)
            conversations_repo.update(
                conn,
                conversation_id,
                {
                    "codex_thread_id": None,
                    "claude_session_id": None,
                    "forked_from_conversation_id": None,
                    "forked_from_external_session_id": None,
                },
                expected_revision=None,
                now_ms=now,
            )
            return {"cleared_messages": len(message_ids)}

        return self.db.write(job, label="reset_conversation")

    # ------------------------------------------------------------ internals

    def _locate(
        self, conn: sqlite3.Connection, actor: Actor, node_id: str
    ) -> tuple[str, sqlite3.Row | None]:
        """Resolve an id to (project_id, conversation_row or None), read-checked."""
        conversation = conversations_repo.get(conn, node_id)
        if conversation is not None:
            require_read(conn, actor, conversation["project_id"])
            return conversation["project_id"], conversation
        require_read(conn, actor, node_id)
        return node_id, None

    def _conversation_or_404(
        self, conn: sqlite3.Connection, actor: Actor, conversation_id: str
    ) -> sqlite3.Row:
        row = conversations_repo.get(conn, conversation_id)
        if row is None:
            raise NotFound("找不到對話")
        require_read(conn, actor, row["project_id"])
        return row

    @staticmethod
    def _path_of(conn: sqlite3.Connection, project_id: str, leaf: str | None = None) -> str:
        names = [row["name"] for row in reversed(projects_repo.ancestors(conn, project_id))]
        if leaf:
            names.append(leaf)
        return " / ".join(names)

    @staticmethod
    def _unique_conversation_name(
        conn: sqlite3.Connection, project_id: str, base: str, *, explicit: bool
    ) -> str:
        """`base`, or "base 2", "base 3"… so siblings never collide.

        An explicitly typed name that is taken is an error the user should see;
        only the generated placeholder is silently numbered.
        """
        if not conversations_repo.live_name_taken(conn, project_id, base):
            return base
        if explicit:
            raise NameConflict(f"這個專案裡已經有「{base}」了")
        counter = 2
        while conversations_repo.live_name_taken(conn, project_id, f"{base} {counter}"):
            counter += 1
        return f"{base} {counter}"

    @staticmethod
    def _fork_name(conn: sqlite3.Connection, project_id: str, base: str) -> str:
        base = _FORK_SUFFIX.sub("", base)
        candidate = f"{base} (分岔)"
        counter = 2
        while conversations_repo.live_name_taken(conn, project_id, candidate):
            candidate = f"{base} (分岔 {counter})"
            counter += 1
        return candidate

    @staticmethod
    def _copy_transcript(
        conn: sqlite3.Connection, source_id: str, target_id: str, now_ms: int
    ) -> int:
        """Copy live messages and their attachment links, preserving order.

        Attachments are re-linked, not re-uploaded: the bytes are shared and
        deduplicated by hash already.
        """
        rows = conn.execute(
            f"SELECT {messages_repo.COLUMNS} FROM messages WHERE conversation_id = ?"
            " AND deleted_at_ms IS NULL ORDER BY sequence_no",
            (source_id,),
        ).fetchall()
        for position, row in enumerate(rows, start=1):
            new_message_id = ids.new_id()
            conn.execute(
                "INSERT INTO messages (id, conversation_id, parent_message_id, sequence_no,"
                " role, content, content_format, agent_id, model, external_event_id,"
                " metadata_json, created_by, created_at_ms, completed_at_ms)"
                " VALUES (?, ?, NULL, ?, ?, ?, ?, ?, ?, NULL, ?, ?, ?, ?)",
                (
                    new_message_id,
                    target_id,
                    position,
                    row["role"],
                    row["content"],
                    row["content_format"],
                    row["agent_id"],
                    row["model"],
                    row["metadata_json"],
                    row["created_by"],
                    row["created_at_ms"],
                    row["completed_at_ms"],
                ),
            )
            conn.execute(
                "INSERT INTO message_attachments (message_id, attachment_id, display_order)"
                " SELECT ?, attachment_id, display_order FROM message_attachments"
                " WHERE message_id = ?",
                (new_message_id, row["id"]),
            )
        return len(rows)

    @staticmethod
    def _reindex(conn: sqlite3.Connection, touched: dict[str, list[str]]) -> None:
        for project_id in touched["projects"]:
            row = projects_repo.get(conn, project_id)
            if row is not None:
                search_repo.index_project(conn, project_id, row["name"], row["prompt"])
        for conversation_id in touched["conversations"]:
            row = conversations_repo.get(conn, conversation_id)
            if row is not None:
                search_repo.reindex_conversation(
                    conn, conversation_id, row["project_id"], row["name"]
                )


# --------------------------------------------------------------- helpers


def _project_siblings(conn: sqlite3.Connection, parent_id: str | None) -> list[tuple[str, str]]:
    return [(row["id"], row["sort_key"]) for row in projects_repo.children(conn, parent_id)]


def _conversation_siblings(conn: sqlite3.Connection, project_id: str) -> list[tuple[str, str]]:
    return [
        (row["id"], row["sort_key"])
        for row in conversations_repo.list_for_project(conn, project_id)
    ]


def _apply_sort_updates(conn: sqlite3.Connection, repo, moves: list[tuple[str, str]]) -> None:
    table = "projects" if repo is projects_repo else "conversations"
    for node_id, sort_key in moves:
        conn.execute(f"UPDATE {table} SET sort_key = ? WHERE id = ?", (sort_key, node_id))


def _fork_position(
    conn: sqlite3.Connection,
    siblings: list[tuple[str, str]],
    source_id: str,
    thread_id: str | None,
) -> int:
    """Just below the source, after any forks of the same thread already there."""
    order = [node_id for node_id, _ in siblings]
    if source_id not in order:
        return len(order)
    position = order.index(source_id) + 1
    while position < len(order):
        row = conversations_repo.get(conn, order[position])
        same_source = row is not None and (
            row["forked_from_conversation_id"] == source_id
            or (thread_id and row["forked_from_external_session_id"] == thread_id)
        )
        if not same_source:
            break
        position += 1
    return position


def _sort_tree(nodes: list[dict[str, Any]]) -> None:
    nodes.sort(key=lambda node: (node["sort_key"], node["name"]))
    for node in nodes:
        node["conversations"].sort(key=lambda item: (item["sort_key"], item["name"]))
        _sort_tree(node["children"])


def _revision_conflict(row: sqlite3.Row) -> None:
    raise RevisionConflict(
        "這個項目已經被其他人修改過，請重新載入後再試一次",
        current_revision=row["revision"],
    )
