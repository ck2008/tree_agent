"""Drives the Codex CLI in non-interactive mode and streams its JSONL events.

One turn == one `codex exec` process. The first turn of a conversation starts a
new Codex thread; every later turn goes through `codex exec resume <thread_id>`,
which is what lets each conversation node keep its own independent context.

Two Windows details this module exists to hide:
  * the prompt is fed through stdin (`-` as the PROMPT argument) so that quotes,
    newlines and shell metacharacters never touch cmd.exe's parser;
  * the child is created with CREATE_NO_WINDOW so no console flashes up.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import threading
from typing import Any, Callable

_ANSI = re.compile(
    r"\x1b\[[0-9;?]*[ -/]*[@-~]"           # CSI: colours, cursor moves, modes
    r"|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)"  # OSC: window titles, terminated by BEL or ST
    r"|\x1b[ -/]*[0-~]"                    # the shorter Fp / Fs / nF escapes
)

CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

# Codex's own sandbox levels, plus one extra choice of ours that turns the
# sandbox off entirely. It is the only mode that works when the working
# directory sits on a mapped network drive — see `sandbox_warning`.
NO_SANDBOX = "no-sandbox"
SANDBOX_MODES = ("read-only", "workspace-write", "danger-full-access", NO_SANDBOX)

SANDBOX_LABELS = {
    "read-only": "read-only — 只能讀檔，不能寫入",
    "workspace-write": "workspace-write — 可寫入工作目錄（網路磁碟不支援）",
    "danger-full-access": "danger-full-access — 最高權限，不限目錄",
    NO_SANDBOX: "no-sandbox — 最高權限，並跳過核准機制",
}

CLAUDE_PERMISSION_DEFAULT = "default"
CLAUDE_PERMISSION_BYPASS = "bypass"
CLAUDE_PERMISSION_LABELS = {
    CLAUDE_PERMISSION_DEFAULT: "default — 依 Claude Code 標準核准",
    CLAUDE_PERMISSION_BYPASS: "bypass — 略過 Claude Code 的核准",
}


def claude_permission_label(mode: str | None) -> str:
    return CLAUDE_PERMISSION_LABELS.get(mode or CLAUDE_PERMISSION_DEFAULT,
                                        CLAUDE_PERMISSION_LABELS[CLAUDE_PERMISSION_DEFAULT])


def claude_permission_from_label(label: str) -> str:
    for mode, text in CLAUDE_PERMISSION_LABELS.items():
        if label == text:
            return mode
    return CLAUDE_PERMISSION_DEFAULT

# Only these two build the restricted-user sandbox, and only they break on a
# mapped network drive. Verified against codex 0.147 on \\\\192.168.1.146\\d$:
# read-only and workspace-write fail with 267, danger-full-access writes fine.
_SANDBOXED_MODES = ("read-only", "workspace-write")

REVIEW_UNCOMMITTED = "uncommitted"
REVIEW_CUSTOM = "custom"

_DRIVE_REMOTE = 4

# Codex delivers these as `error` items, but they are advisory notices rather
# than failures. Showing them in red would cry wolf on every single turn.
_BENIGN_NOTICES = ("Skill descriptions were shortened",)

# item.type -> (display role, JSON field holding the interesting text)
_SIMPLE_ITEMS = {
    "agent_message": ("agent", "text"),
    "reasoning": ("reasoning", "text"),
    "error": ("error", "message"),
}


class CodexNotFound(RuntimeError):
    pass


class ClaudeNotFound(RuntimeError):
    pass


def find_codex(path: str | None = None) -> str:
    """Absolute path to the Codex CLI launcher (codex.CMD on Windows)."""
    exe = path if path and os.path.isfile(path) else shutil.which("codex")
    if not exe:
        raise CodexNotFound(
            "找不到 codex CLI。請先安裝：npm install -g @openai/codex，"
            "並確認 codex 在 PATH 中。"
        )
    return exe


def find_claude(path: str | None = None) -> str:
    """Resolve Claude Code from a saved path or the current PATH."""
    exe = path if path and os.path.isfile(path) else shutil.which("claude")
    if not exe:
        raise ClaudeNotFound("找不到 Claude Code CLI。請安裝後確認 claude 在 PATH 中。")
    return exe


def codex_version(path: str | None = None) -> str:
    try:
        out = subprocess.run(
            [find_codex(path), "--version"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
            creationflags=CREATE_NO_WINDOW,
        )
        return (out.stdout or out.stderr or "").strip().splitlines()[0]
    except (CodexNotFound, OSError, subprocess.SubprocessError, IndexError):
        return ""


def claude_version(path: str | None = None) -> str:
    try:
        out = subprocess.run(
            [find_claude(path), "--version"], capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=30,
            creationflags=CREATE_NO_WINDOW,
        )
        return (out.stdout or out.stderr or "").strip().splitlines()[0]
    except (ClaudeNotFound, OSError, subprocess.SubprocessError, IndexError):
        return ""


def archive_session(thread_id: str) -> None:
    """Archive a Codex session, so deleting a conversation here does not leave
    an orphan behind in ~/.codex/sessions. Fire-and-forget: a failure is not
    worth interrupting the delete the user already confirmed."""
    if not thread_id:
        return

    def run() -> None:
        try:
            subprocess.run(
                [find_codex(), "archive", thread_id],
                capture_output=True,
                timeout=60,
                creationflags=CREATE_NO_WINDOW,
            )
        except (CodexNotFound, OSError, subprocess.SubprocessError):
            pass

    threading.Thread(target=run, daemon=True).start()


def clean_output(text: str) -> str:
    """Strip terminal control codes and normalise line endings.

    PowerShell colours its table headers, so captured command output arrives
    full of `\\x1b[32;1m`. `NO_COLOR=1` in the child environment stops most of
    it at the source, but anything that still slips through would render as
    literal garbage in a Text widget — and old transcripts already contain it.
    """
    if not text:
        return text
    return _ANSI.sub("", text).replace("\r\n", "\n").replace("\r", "\n")


def is_network_path(path: str | None) -> bool:
    """True for a UNC path or a drive letter mapped to a network share."""
    if os.name != "nt" or not path:
        return False
    if path.startswith("\\\\") or path.startswith("//"):
        return True
    drive = os.path.splitdrive(os.path.abspath(path))[0]
    if not drive:
        return False
    try:
        import ctypes

        return ctypes.windll.kernel32.GetDriveTypeW(drive + "\\") == _DRIVE_REMOTE
    except Exception:
        return False


def sandbox_label(mode: str | None) -> str:
    return SANDBOX_LABELS.get(mode or "", mode or "")


def sandbox_from_label(label: str) -> str:
    """Turn a combobox label back into the stored mode name."""
    for mode, text in SANDBOX_LABELS.items():
        if text == label:
            return mode
    return label  # already a bare mode name


def sandbox_warning(cwd: str | None, sandbox: str | None) -> str | None:
    """Explain, before running, a combination that is known to fail.

    The sandboxed modes launch commands as a separate restricted user via
    `CreateProcessWithLogonW`. Drive-letter mappings belong to a logon session,
    so `F:\\...` does not resolve for that user and every command dies with
    `ERROR_DIRECTORY (267)`. The full-access modes build no sandbox, so they
    run as you — where the mapping does exist — and work fine.
    """
    if sandbox not in _SANDBOXED_MODES or not is_network_path(cwd):
        return None
    return (
        "工作目錄在網路磁碟上，Codex 的沙箱無法在這裡執行任何指令"
        "（CreateProcessWithLogonW 回傳 267）。請把沙箱模式改為"
        "「danger-full-access」，或改用本機磁碟上的路徑。"
    )


def describe_item(item: dict[str, Any]) -> tuple[str, str] | None:
    """Turn a Codex `item.completed` payload into (role, text) for display.

    Returns None for items with nothing worth showing.
    """
    itype = item.get("type", "")

    if itype in _SIMPLE_ITEMS:
        role, field = _SIMPLE_ITEMS[itype]
        text = (item.get(field) or "").strip()
        if not text:
            return None
        if role == "error" and any(n in text for n in _BENIGN_NOTICES):
            return "notice", text
        return role, text

    if itype == "command_execution":
        command = item.get("command") or ""
        exit_code = item.get("exit_code")
        output = clean_output(item.get("aggregated_output") or "").rstrip()
        head = f"$ {command}"
        if exit_code not in (None, 0):
            head += f"   (exit {exit_code})"
        if output:
            if len(output) > 4000:
                output = output[:4000] + "\n… (輸出已截斷)"
            head += "\n" + output
        return "tool", head

    if itype == "file_change":
        lines = []
        for change in item.get("changes") or []:
            kind = change.get("kind") or change.get("type") or "update"
            lines.append(f"  {kind:<6} {change.get('path', '')}")
        return ("tool", "檔案變更\n" + "\n".join(lines)) if lines else None

    if itype == "mcp_tool_call":
        label = f"{item.get('server', '')}.{item.get('tool', '')}".strip(".")
        status = item.get("status") or ""
        return "tool", f"MCP {label} {status}".strip()

    if itype == "web_search":
        query = item.get("query") or ""
        return ("tool", f"網路搜尋: {query}") if query else None

    if itype == "todo_list":
        lines = []
        for todo in item.get("items") or []:
            mark = "x" if todo.get("completed") else " "
            lines.append(f"  [{mark}] {todo.get('text', '')}")
        return ("tool", "待辦清單\n" + "\n".join(lines)) if lines else None

    # Unknown item type: show it rather than swallowing it, but keep it terse.
    return "tool", f"{itype}: {json.dumps(item, ensure_ascii=False)[:800]}"


class Turn:
    """A single running `codex exec` invocation.

    `emit` is called from worker threads, so the caller is expected to hand the
    events straight to a queue and drain it on the UI thread.
    """

    def __init__(
        self,
        prompt: str,
        cwd: str,
        emit: Callable[[dict[str, Any]], None],
        thread_id: str | None = None,
        model: str | None = None,
        sandbox: str | None = None,
        fork_from: str | None = None,
        images: list[str] | None = None,
        review: str | None = None,
        executable: str | None = None,
    ) -> None:
        self.images = list(images or ())
        # None for an ordinary turn; REVIEW_UNCOMMITTED reviews the working tree
        # (Codex rejects a prompt alongside it); REVIEW_CUSTOM sends the prompt
        # as review instructions instead.
        self.review = review
        self.prompt = prompt
        self.cwd = cwd
        self.emit = emit
        self.thread_id = thread_id
        self.fork_from = fork_from
        self.model = model
        self.sandbox = sandbox
        self.executable = executable
        self.proc: subprocess.Popen[str] | None = None
        self._cancelled = False
        self._lock = threading.Lock()

    # ------------------------------------------------------------- command

    def build_command(self) -> list[str]:
        cmd = [find_codex(self.executable), "exec", "--json", "--skip-git-repo-check"]
        if self.sandbox == NO_SANDBOX:
            cmd.append("--dangerously-bypass-approvals-and-sandbox")
        elif self.sandbox:
            cmd += ["-s", self.sandbox]
        if self.model:
            cmd += ["-m", self.model]
        # `-i` is variadic on `exec`, so a bare `-i a b` would swallow the prompt
        # as a second file. Repeating the flag keeps one file per occurrence.
        for image in self.images:
            cmd += ["-i", image]
        # Sub-commands must come after the shared options.
        if self.review == REVIEW_UNCOMMITTED:
            # `--uncommitted` and a PROMPT are mutually exclusive, so no `-`.
            return cmd + ["review", "--uncommitted"]
        if self.review == REVIEW_CUSTOM:
            return cmd + ["review", "-"]
        if self.thread_id:
            cmd += ["resume", self.thread_id]
        elif self.fork_from:
            # Branch off another thread. Codex hands back a *new* thread id in
            # `thread.started`, and the source thread is left untouched, so from
            # the next turn on this conversation resumes its own thread.
            cmd += ["fork", self.fork_from]
        cmd.append("-")  # read the prompt from stdin
        return cmd

    def describe_command(self) -> str:
        return subprocess.list2cmdline(self.build_command())

    # --------------------------------------------------------------- run

    def start(self) -> None:
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self) -> None:
        try:
            cmd = self.build_command()
        except CodexNotFound as exc:
            self.emit({"kind": "item", "role": "error", "text": str(exc)})
            self.emit({"kind": "done", "returncode": -1})
            return

        workdir = self.cwd if self.cwd and os.path.isdir(self.cwd) else os.path.expanduser("~")
        if workdir != self.cwd:
            self.emit(
                {"kind": "log", "text": f"工作目錄 {self.cwd!r} 不存在，改用 {workdir}"}
            )

        # Codex forwards this to the shells it spawns, which stops PowerShell
        # from colouring its output — cleaner on screen, and fewer wasted
        # tokens in the model's own view of the command results.
        env = dict(os.environ)
        env.setdefault("NO_COLOR", "1")

        try:
            proc = subprocess.Popen(
                cmd,
                cwd=workdir,
                env=env,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                creationflags=CREATE_NO_WINDOW,
            )
        except OSError as exc:
            self.emit({"kind": "item", "role": "error", "text": f"無法啟動 codex: {exc}"})
            self.emit({"kind": "done", "returncode": -1})
            return

        with self._lock:
            self.proc = proc
            if self._cancelled:  # cancel() landed before Popen returned
                self._kill(proc)

        try:
            assert proc.stdin is not None
            # `review --uncommitted` takes no prompt, so there is no `-` to read
            # stdin — writing anyway risks blocking on a pipe nobody drains.
            if self.review != REVIEW_UNCOMMITTED:
                proc.stdin.write(self.prompt)
            proc.stdin.close()
        except OSError:
            pass  # the process died early; stderr will explain why

        err_thread = threading.Thread(target=self._drain_stderr, args=(proc,), daemon=True)
        err_thread.start()

        assert proc.stdout is not None
        for line in proc.stdout:
            self._handle_stdout_line(line)

        returncode = proc.wait()
        err_thread.join(timeout=2)
        self.emit(
            {
                "kind": "done",
                "returncode": returncode,
                "cancelled": self._cancelled,
            }
        )

    def _handle_stdout_line(self, line: str) -> None:
        line = line.strip()
        if not line:
            return
        if not line.startswith("{"):
            self.emit({"kind": "log", "text": line})
            return
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            self.emit({"kind": "log", "text": line})
            return

        etype = event.get("type")
        if etype == "thread.started":
            self.thread_id = event.get("thread_id")
            self.emit({"kind": "thread", "thread_id": self.thread_id})
        elif etype == "turn.started":
            self.emit({"kind": "turn_start"})
        elif etype == "item.completed":
            described = describe_item(event.get("item") or {})
            if described:
                role, text = described
                self.emit({"kind": "item", "role": role, "text": text})
        elif etype in ("item.started", "item.updated"):
            # Progress echoes of an item we will render in full on completion.
            pass
        elif etype == "turn.completed":
            self.emit({"kind": "usage", "usage": event.get("usage") or {}})
        elif etype in ("turn.failed", "error"):
            detail = event.get("error") or event.get("message") or event
            if isinstance(detail, dict):
                detail = detail.get("message") or json.dumps(detail, ensure_ascii=False)
            self.emit({"kind": "item", "role": "error", "text": str(detail)})
        else:
            self.emit({"kind": "log", "text": f"[{etype}] {line[:400]}"})

    def _drain_stderr(self, proc: subprocess.Popen[str]) -> None:
        assert proc.stderr is not None
        for line in proc.stderr:
            line = clean_output(line).rstrip()
            if line:
                self.emit({"kind": "log", "text": line})

    # ------------------------------------------------------------- cancel

    def cancel(self) -> None:
        with self._lock:
            self._cancelled = True
            proc = self.proc
        if proc is not None:
            self._kill(proc)

    @staticmethod
    def _kill(proc: subprocess.Popen[str]) -> None:
        if proc.poll() is not None:
            return
        if os.name == "nt":
            # codex.CMD spawns node as a child; terminate() would only reap the
            # shim and leave the real agent running.
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                capture_output=True,
                creationflags=CREATE_NO_WINDOW,
            )
        else:
            proc.terminate()


class ClaudeTurn:
    """One non-interactive Claude Code turn, streamed as Claude JSONL events."""

    def __init__(self, prompt: str, cwd: str, emit: Callable[[dict[str, Any]], None],
                 session_id: str | None = None, model: str | None = None,
                 executable: str | None = None, permission_mode: str | None = None) -> None:
        self.prompt, self.cwd, self.emit = prompt, cwd, emit
        self.session_id, self.model, self.executable = session_id, model, executable
        self.permission_mode = permission_mode or CLAUDE_PERMISSION_DEFAULT
        self.proc: subprocess.Popen[str] | None = None
        self._cancelled = False
        self._lock = threading.Lock()

    def build_command(self) -> list[str]:
        cmd = [find_claude(self.executable), "-p", "--output-format", "stream-json", "--verbose"]
        if self.session_id:
            cmd += ["--resume", self.session_id]
        if self.model:
            cmd += ["--model", self.model]
        if self.permission_mode == CLAUDE_PERMISSION_BYPASS:
            # Print mode cannot present an approval dialog.  This explicit
            # flag is Claude Code's non-interactive opt-in to run tools.
            cmd += ["--dangerously-skip-permissions"]
        return cmd

    def describe_command(self) -> str:
        return subprocess.list2cmdline(self.build_command())

    def start(self) -> None:
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self) -> None:
        try:
            cmd = self.build_command()
        except ClaudeNotFound as exc:
            self.emit({"kind": "item", "role": "error", "text": str(exc)})
            self.emit({"kind": "done", "returncode": -1})
            return
        workdir = self.cwd if self.cwd and os.path.isdir(self.cwd) else os.path.expanduser("~")
        try:
            proc = subprocess.Popen(
                cmd, cwd=workdir, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, encoding="utf-8", errors="replace", bufsize=1,
                creationflags=CREATE_NO_WINDOW,
            )
        except OSError as exc:
            self.emit({"kind": "item", "role": "error", "text": f"無法啟動 Claude Code: {exc}"})
            self.emit({"kind": "done", "returncode": -1})
            return
        with self._lock:
            self.proc = proc
            if self._cancelled:
                Turn._kill(proc)
        try:
            assert proc.stdin is not None
            proc.stdin.write(self.prompt)
            proc.stdin.close()
        except OSError:
            pass
        err_thread = threading.Thread(target=self._drain_stderr, args=(proc,), daemon=True)
        err_thread.start()
        self.emit({"kind": "turn_start"})
        assert proc.stdout is not None
        for line in proc.stdout:
            self._handle_stdout_line(line)
        returncode = proc.wait()
        err_thread.join(timeout=2)
        self.emit({"kind": "done", "returncode": returncode, "cancelled": self._cancelled})

    def _handle_stdout_line(self, line: str) -> None:
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            if line.strip():
                self.emit({"kind": "log", "text": clean_output(line).rstrip()})
            return
        etype = event.get("type")
        if etype == "system" and event.get("subtype") == "init":
            session_id = event.get("session_id")
            if session_id:
                self.session_id = session_id
                self.emit({"kind": "session", "session_id": session_id})
        elif etype == "assistant":
            for item in (event.get("message") or {}).get("content") or []:
                if item.get("type") == "text" and item.get("text"):
                    self.emit({"kind": "item", "role": "agent", "text": item["text"]})
                elif item.get("type") == "tool_use":
                    self.emit({"kind": "item", "role": "tool", "text": f"工具：{item.get('name', '')}"})
        elif etype == "result":
            if event.get("is_error"):
                self.emit({"kind": "item", "role": "error", "text": str(event.get("result") or "Claude Code 執行失敗")})
            usage = event.get("usage")
            if isinstance(usage, dict):
                self.emit({"kind": "usage", "usage": usage})
        elif etype == "error":
            self.emit({"kind": "item", "role": "error", "text": str(event.get("error") or event)})

    def _drain_stderr(self, proc: subprocess.Popen[str]) -> None:
        assert proc.stderr is not None
        for line in proc.stderr:
            if line.strip():
                self.emit({"kind": "log", "text": clean_output(line).rstrip()})

    def cancel(self) -> None:
        with self._lock:
            self._cancelled = True
            proc = self.proc
        if proc is not None:
            Turn._kill(proc)
