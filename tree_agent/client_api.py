"""HTTP client for the shared workspace service.

Deliberately built on the standard library: the desktop app already ships with
no networking dependency, and adding one to talk to its own server would be a
poor trade. The surface mirrors `tree_agent.server.api` one method per endpoint,
so a call site reads the same as the route it hits.

Writes that create something take an `Idempotency-Key`, generated per logical
operation rather than per attempt, so retrying after a dropped connection
returns the original result instead of creating a second copy.
"""

from __future__ import annotations

import json
import os
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from typing import Any, Iterator

from . import store

DEFAULT_TIMEOUT = 30.0
UPLOAD_TIMEOUT = 300.0


class ApiError(RuntimeError):
    """A structured error from the service, or a transport failure."""

    def __init__(self, status: int, code: str, detail: str, payload: dict[str, Any] | None = None):
        super().__init__(f"[{status}] {detail}")
        self.status = status
        self.code = code
        self.detail = detail
        self.payload = payload or {}

    @property
    def is_conflict(self) -> bool:
        return self.status == 409

    @property
    def current_revision(self) -> int | None:
        value = self.payload.get("current_revision")
        return value if isinstance(value, int) else None


class WorkspaceClient:
    def __init__(
        self,
        base_url: str,
        *,
        token: str | None = None,
        timeout: float = DEFAULT_TIMEOUT,
        verify_tls: bool = True,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.timeout = timeout
        self._ssl_context = None if verify_tls else ssl._create_unverified_context()

    # ------------------------------------------------------------ plumbing

    def _request(
        self,
        method: str,
        path: str,
        *,
        body: Any = None,
        raw: bytes | None = None,
        params: dict[str, Any] | None = None,
        idempotency_key: str | None = None,
        timeout: float | None = None,
    ) -> Any:
        url = f"{self.base_url}{path}"
        if params:
            filtered = {k: v for k, v in params.items() if v is not None}
            if filtered:
                url = f"{url}?{urllib.parse.urlencode(filtered)}"

        headers = {"Accept": "application/json"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        if idempotency_key:
            headers["Idempotency-Key"] = idempotency_key

        data: bytes | None = raw
        if raw is not None:
            headers["Content-Type"] = "application/octet-stream"
        elif body is not None:
            data = json.dumps(body, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"

        request = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(
                request, timeout=timeout or self.timeout, context=self._ssl_context
            ) as response:
                payload = response.read()
        except urllib.error.HTTPError as exc:
            raise _to_api_error(exc) from None
        except urllib.error.URLError as exc:
            raise ApiError(0, "unreachable", f"無法連線到 {self.base_url}：{exc.reason}") from None
        if not payload:
            return None
        return json.loads(payload.decode("utf-8"))

    def _stream(self, path: str, chunk_size: int = 1024 * 1024) -> tuple[dict[str, str], Iterator[bytes]]:
        request = urllib.request.Request(f"{self.base_url}{path}", method="GET")
        if self.token:
            request.add_header("Authorization", f"Bearer {self.token}")
        try:
            response = urllib.request.urlopen(
                request, timeout=UPLOAD_TIMEOUT, context=self._ssl_context
            )
        except urllib.error.HTTPError as exc:
            raise _to_api_error(exc) from None

        def chunks() -> Iterator[bytes]:
            with response:
                while True:
                    block = response.read(chunk_size)
                    if not block:
                        break
                    yield block

        return dict(response.headers), chunks()

    # ---------------------------------------------------------------- auth

    def health(self) -> dict[str, Any]:
        return self._request("GET", "/api/health")

    def bootstrap(
        self, *, token: str, username: str, password: str, email: str, display_name: str = ""
    ) -> dict:
        return self._request(
            "POST",
            "/api/auth/bootstrap",
            body={
                "token": token,
                "username": username,
                "password": password,
                "email": email,
                "display_name": display_name,
            },
            idempotency_key=new_key(),
        )

    def login(self, username: str, password: str) -> dict[str, Any]:
        result = self._request(
            "POST", "/api/auth/login", body={"username": username, "password": password}, idempotency_key=new_key()
        )
        self.token = result["token"]
        return result["user"]

    def logout(self) -> None:
        try:
            self._request("POST", "/api/auth/logout", idempotency_key=new_key())
        finally:
            self.token = None

    def request_password_reset(self, email: str) -> None:
        self._request(
            "POST", "/api/auth/password-reset/request", body={"email": email},
            idempotency_key=new_key(),
        )

    def confirm_password_reset(self, *, email: str, code: str, password: str) -> None:
        self._request(
            "POST", "/api/auth/password-reset/confirm",
            body={"email": email, "code": code, "password": password},
            idempotency_key=new_key(),
        )

    def me(self) -> dict[str, Any]:
        return self._request("GET", "/api/auth/me")

    def change_password(self, current_password: str, password: str) -> None:
        self._request(
            "POST",
            "/api/auth/password",
            body={"current_password": current_password, "password": password}, idempotency_key=new_key(),
        )

    def request_email_verification(self, email: str) -> None:
        self._request(
            "POST", "/api/auth/email-verification/request", body={"email": email},
            idempotency_key=new_key(),
        )

    def confirm_email_verification(self, *, email: str, code: str) -> dict[str, Any]:
        return self._request(
            "POST", "/api/auth/email-verification/confirm", body={"email": email, "code": code},
            idempotency_key=new_key(),
        )

    # --------------------------------------------------------------- users

    def users(self) -> list[dict[str, Any]]:
        return self._request("GET", "/api/users")

    def create_user(
        self, *, username: str, password: str, email: str, display_name: str = "", role: str = "member"
    ) -> dict[str, Any]:
        return self._request(
            "POST",
            "/api/users",
            body={
                "username": username,
                "password": password,
                "email": email,
                "display_name": display_name,
                "role": role,
            },
            idempotency_key=new_key(),
        )

    def set_user_active(self, user_id: str, active: bool) -> dict[str, Any]:
        return self._request("POST", f"/api/users/{user_id}/active", body={"is_active": active}, idempotency_key=new_key())

    def set_user_role(self, user_id: str, role: str) -> dict[str, Any]:
        return self._request("POST", f"/api/users/{user_id}/role", body={"role": role}, idempotency_key=new_key())

    def reset_password(self, user_id: str, password: str) -> None:
        self._request("POST", f"/api/users/{user_id}/password", body={"password": password}, idempotency_key=new_key())

    # ------------------------------------------------------------ the tree

    def tree(self) -> dict[str, Any]:
        return self._request("GET", "/api/tree")

    def project(self, project_id: str) -> dict[str, Any]:
        return self._request("GET", f"/api/projects/{project_id}")

    def create_project(
        self,
        *,
        parent_id: str | None,
        name: str,
        index: int | None = None,
        settings: dict[str, Any] | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        return self._request(
            "POST",
            "/api/projects",
            body={"parent_id": parent_id, "name": name, "index": index, "settings": settings},
            idempotency_key=idempotency_key or new_key(),
        )

    def update_project(self, project_id: str, *, revision: int, **fields: Any) -> dict[str, Any]:
        return self._request(
            "PATCH", f"/api/projects/{project_id}", body={"revision": revision, "fields": fields}, idempotency_key=new_key()
        )

    def move_project(
        self, project_id: str, *, revision: int, parent_id: str | None, index: int | None = None
    ) -> dict[str, Any]:
        return self._request(
            "POST",
            f"/api/projects/{project_id}/move",
            body={"revision": revision, "parent_id": parent_id, "index": index}, idempotency_key=new_key(),
        )

    def set_expanded(self, project_id: str, expanded: bool) -> None:
        self._request("POST", f"/api/projects/{project_id}/expanded", body={"is_expanded": expanded}, idempotency_key=new_key())

    def delete_project(self, project_id: str, *, revision: int) -> dict[str, Any]:
        return self._request(
            "DELETE", f"/api/projects/{project_id}", params={"revision": revision}, idempotency_key=new_key()
        )

    def restore_project(self, project_id: str) -> dict[str, Any]:
        return self._request("POST", f"/api/projects/{project_id}/restore", idempotency_key=new_key())

    def memberships(self, project_id: str) -> list[dict[str, Any]]:
        return self._request("GET", f"/api/projects/{project_id}/memberships")

    def grant(self, project_id: str, *, user_id: str, permission: str) -> list[dict[str, Any]]:
        return self._request(
            "POST",
            f"/api/projects/{project_id}/memberships",
            body={"user_id": user_id, "permission": permission}, idempotency_key=new_key(),
        )

    def revoke(self, project_id: str, user_id: str) -> list[dict[str, Any]]:
        return self._request("DELETE", f"/api/projects/{project_id}/memberships/{user_id}", idempotency_key=new_key())

    # ------------------------------------------- inherited settings, paths

    def settings_for(self, node_id: str) -> dict[str, Any]:
        return self._request("GET", f"/api/nodes/{node_id}/settings")

    def instructions_for(self, node_id: str) -> str:
        return self._request("GET", f"/api/nodes/{node_id}/instructions")["instructions"]

    def path_of(self, node_id: str) -> str:
        return self._request("GET", f"/api/nodes/{node_id}/path")["path"]

    def usage_of(self, node_id: str) -> dict[str, int]:
        return self._request("GET", f"/api/nodes/{node_id}/usage")

    # ------------------------------------------------------- conversations

    def conversations(self, project_id: str) -> list[dict[str, Any]]:
        return self._request("GET", f"/api/projects/{project_id}/conversations")

    def conversation(self, conversation_id: str) -> dict[str, Any]:
        return self._request("GET", f"/api/conversations/{conversation_id}")

    def create_conversation(
        self,
        *,
        project_id: str,
        name: str | None = None,
        agent_id: str = "codex",
        model: str | None = None,
        index: int | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        return self._request(
            "POST",
            "/api/conversations",
            body={
                "project_id": project_id,
                "name": name,
                "agent_id": agent_id,
                "model": model,
                "index": index,
            },
            idempotency_key=idempotency_key or new_key(),
        )

    def update_conversation(
        self, conversation_id: str, *, revision: int, **fields: Any
    ) -> dict[str, Any]:
        return self._request(
            "PATCH",
            f"/api/conversations/{conversation_id}",
            body={"revision": revision, "fields": fields}, idempotency_key=new_key(),
        )

    def move_conversation(
        self, conversation_id: str, *, revision: int, project_id: str, index: int | None = None
    ) -> dict[str, Any]:
        return self._request(
            "POST",
            f"/api/conversations/{conversation_id}/move",
            body={"revision": revision, "project_id": project_id, "index": index}, idempotency_key=new_key(),
        )

    def fork_conversation(
        self, conversation_id: str, *, idempotency_key: str | None = None
    ) -> dict[str, Any]:
        return self._request(
            "POST",
            f"/api/conversations/{conversation_id}/fork",
            idempotency_key=idempotency_key or new_key(),
        )

    def set_runner_state(
        self,
        conversation_id: str,
        *,
        codex_thread_id: str | None = None,
        claude_session_id: str | None = None,
    ) -> dict[str, Any]:
        return self._request(
            "POST",
            f"/api/conversations/{conversation_id}/runner",
            body={"codex_thread_id": codex_thread_id, "claude_session_id": claude_session_id}, idempotency_key=new_key(),
        )

    def reset_conversation(self, conversation_id: str) -> dict[str, Any]:
        return self._request("POST", f"/api/conversations/{conversation_id}/reset", idempotency_key=new_key())

    def delete_conversation(
        self, conversation_id: str, *, revision: int
    ) -> dict[str, Any]:
        return self._request(
            "DELETE", f"/api/conversations/{conversation_id}", params={"revision": revision}, idempotency_key=new_key()
        )

    def restore_conversation(self, conversation_id: str) -> dict[str, Any]:
        return self._request("POST", f"/api/conversations/{conversation_id}/restore", idempotency_key=new_key())

    def cancel_turn(self, conversation_id: str, note: str = "（已停止）") -> dict[str, Any]:
        return self._request(
            "POST", f"/api/conversations/{conversation_id}/cancel", body={"note": note}, idempotency_key=new_key()
        )

    # ------------------------------------------------------------ messages

    def messages(
        self,
        conversation_id: str,
        *,
        after_sequence_no: int | None = None,
        before_sequence_no: int | None = None,
        limit: int = 200,
    ) -> dict[str, Any]:
        return self._request(
            "GET",
            f"/api/conversations/{conversation_id}/messages",
            params={
                "after_sequence_no": after_sequence_no,
                "before_sequence_no": before_sequence_no,
                "limit": limit,
            },
        )

    def iter_messages(self, conversation_id: str, *, page: int = 200) -> Iterator[dict[str, Any]]:
        """Walk a whole transcript a page at a time.

        The service refuses to return an unbounded transcript, and for good
        reason — this is how a client asks for all of it without holding a read
        snapshot open for the duration.
        """
        cursor: int | None = None
        while True:
            batch = self.messages(conversation_id, after_sequence_no=cursor, limit=page)
            for message in batch["messages"]:
                yield message
            if not batch["has_more"] or batch["next_after_sequence_no"] is None:
                return
            cursor = batch["next_after_sequence_no"]

    def append_message(
        self,
        conversation_id: str,
        *,
        role: str,
        content: str = "",
        content_format: str = "plain",
        agent_id: str | None = None,
        model: str | None = None,
        external_event_id: str | None = None,
        metadata: dict[str, Any] | None = None,
        attachment_ids: list[str] | None = None,
        completed: bool = False,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        return self._request(
            "POST",
            f"/api/conversations/{conversation_id}/messages",
            body={
                "role": role,
                "content": content,
                "content_format": content_format,
                "agent_id": agent_id,
                "model": model,
                "external_event_id": external_event_id,
                "metadata": metadata,
                "attachment_ids": attachment_ids,
                "completed": completed,
            },
            idempotency_key=idempotency_key or new_key(),
        )

    def append_delta(self, message_id: str, delta: str) -> dict[str, Any]:
        return self._request("POST", f"/api/messages/{message_id}/append", body={"delta": delta}, idempotency_key=new_key())

    def complete_message(
        self,
        message_id: str,
        *,
        content: str | None = None,
        usage: dict[str, Any] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return self._request(
            "POST",
            f"/api/messages/{message_id}/complete",
            body={"content": content, "usage": usage, "metadata": metadata}, idempotency_key=new_key(),
        )

    def delete_message(self, message_id: str) -> dict[str, Any]:
        return self._request("DELETE", f"/api/messages/{message_id}", idempotency_key=new_key())

    def add_tool_call(
        self,
        message_id: str,
        *,
        tool_name: str,
        status: str = "running",
        payload: dict[str, Any] | None = None,
        output_text: str = "",
        error_text: str | None = None,
    ) -> dict[str, Any]:
        return self._request(
            "POST",
            f"/api/messages/{message_id}/tool-calls",
            body={
                "tool_name": tool_name,
                "status": status,
                "input": payload,
                "output_text": output_text,
                "error_text": error_text,
            }, idempotency_key=new_key(),
        )

    def update_tool_call(
        self,
        tool_call_id: str,
        *,
        status: str | None = None,
        output_text: str | None = None,
        error_text: str | None = None,
    ) -> dict[str, Any]:
        return self._request(
            "PATCH",
            f"/api/tool-calls/{tool_call_id}",
            body={"status": status, "output_text": output_text, "error_text": error_text}, idempotency_key=new_key(),
        )

    # --------------------------------------------------------- attachments

    def upload_file(
        self,
        *,
        conversation_id: str,
        path: str,
        message_id: str | None = None,
        mime_type: str | None = None,
    ) -> dict[str, Any]:
        """Send a file from disk, one chunk at a time. Never buffers the whole file."""
        import hashlib
        import mimetypes

        size = os.path.getsize(path)
        digest = hashlib.sha256()
        with open(path, "rb") as handle:
            while True:
                block = handle.read(1024 * 1024)
                if not block:
                    break
                digest.update(block)

        started = self._request(
            "POST",
            "/api/attachments/uploads",
            body={
                "conversation_id": conversation_id,
                "file_name": os.path.basename(path),
                "mime_type": mime_type or mimetypes.guess_type(path)[0] or "application/octet-stream",
                "byte_size": size,
                "sha256": digest.hexdigest(),
                "message_id": message_id,
            },
            idempotency_key=new_key(),
        )
        chunk_size = started["chunk_size"]
        with open(path, "rb") as handle:
            for chunk_no in range(started["chunk_count"]):
                block = handle.read(chunk_size)
                self._request(
                    "PUT",
                    f"/api/attachments/uploads/{started['upload_id']}/chunks/{chunk_no}",
                    raw=block,
                    timeout=UPLOAD_TIMEOUT,
                    idempotency_key=new_key(),
                )
        return self._request(
            "POST",
            f"/api/attachments/uploads/{started['upload_id']}/commit",
            body={"message_id": message_id},
            idempotency_key=new_key(),
        )

    def attachment(self, attachment_id: str) -> dict[str, Any]:
        return self._request("GET", f"/api/attachments/{attachment_id}")

    def download_attachment(self, attachment_id: str, destination: str) -> str:
        """Stream an attachment to disk, verifying the hash the server sent."""
        import hashlib

        headers, chunks = self._stream(f"/api/attachments/{attachment_id}/content")
        digest = hashlib.sha256()
        os.makedirs(os.path.dirname(os.path.abspath(destination)) or ".", exist_ok=True)
        with open(destination, "wb") as handle:
            for block in chunks:
                digest.update(block)
                handle.write(block)
        expected = headers.get("X-Attachment-Sha256")
        if expected and digest.hexdigest() != expected:
            os.remove(destination)
            raise ApiError(0, "checksum_mismatch", "下載的附件與伺服器的 SHA-256 不符")
        return destination

    def detach_attachment(self, message_id: str, attachment_id: str) -> dict[str, Any]:
        return self._request("DELETE", f"/api/messages/{message_id}/attachments/{attachment_id}",
                             idempotency_key=new_key())

    # -------------------------------------------------------------- search

    def search(
        self, query: str, *, kinds: tuple[str, ...] = ("project", "conversation", "message"),
        limit: int = 20, offset: int = 0,
    ) -> dict[str, Any]:
        return self._request(
            "GET", "/api/search",
            params={"q": query, "kinds": ",".join(kinds), "limit": limit, "offset": offset},
        )

    # --------------------------------------------------------------- admin

    def get_mail_settings(self) -> dict[str, Any]:
        return self._request("GET", "/api/admin/mail-settings")

    def update_mail_settings(
        self,
        *,
        host: str,
        port: int,
        from_address: str,
        security: str,
        username: str | None = None,
        password: str | None = None,
    ) -> dict[str, Any]:
        return self._request(
            "PUT",
            "/api/admin/mail-settings",
            body={
                "host": host,
                "port": port,
                "from_address": from_address,
                "encryption": security,
                "username": username or "",
                "password": password,
            },
            idempotency_key=new_key(),
        )

    def test_mail_settings(self, recipient: str) -> dict[str, Any]:
        return self._request(
            "POST", "/api/admin/mail-settings/test", body={"recipient": recipient},
            idempotency_key=new_key(),
        )

    def stats(self) -> dict[str, Any]:
        return self._request("GET", "/api/admin/stats")

    def sweep(self) -> dict[str, Any]:
        return self._request("POST", "/api/admin/sweep", idempotency_key=new_key())

    def purge(self, *, retention_days: int = 30, dry_run: bool = True) -> dict[str, Any]:
        return self._request("POST", "/api/admin/purge", body={"retention_days": retention_days, "dry_run": dry_run}, idempotency_key=new_key())

    def backup(self, destination: str) -> dict[str, Any]:
        return self._request("POST", "/api/admin/backup", body={"destination": destination}, idempotency_key=new_key())

    def import_legacy_workspace(self, source_path: str, *, parent_project_id: str | None = None, dry_run: bool = False) -> dict[str, Any]:
        return self._request("POST", "/api/admin/import-legacy", body={"source_path": source_path, "parent_project_id": parent_project_id, "dry_run": dry_run}, timeout=UPLOAD_TIMEOUT, idempotency_key=new_key())


class RemoteWorkspace:
    """A small ``store.Workspace``-shaped cache backed by :class:`WorkspaceClient`.

    The Tk UI predates the service and deliberately talks to a document model.
    Keeping that presentation cache at this boundary lets the UI remain
    responsive without quietly falling back to ``workspace.json``.  Only UI
    preferences and machine-specific runner paths are stored locally; projects,
    conversations, messages and runner session ids are always read/written via
    the service.
    """

    REPLACE_ATTEMPTS = 8
    REPLACE_BACKOFF = 0.03

    def __init__(self, client: WorkspaceClient, home: str) -> None:
        self.client = client
        self.home = home
        self.path = f"{client.base_url}/api/tree"
        self._prefs_path = os.path.join(home, "desktop.json")
        self.last_local_save_error: OSError | None = None
        self._prefs = self._load_prefs()
        self._execution = dict(self._prefs.get("execution") or {})
        self.data: dict[str, Any] = {
            "ui": dict(self._prefs.get("ui") or {}),
            "agents": dict(self._prefs.get("agents") or {}),
            "defaults": {},
            "projects": [],
        }
        self._parents: dict[str, dict[str, Any] | None] = {}
        self._message_ids: dict[str, list[str]] = {}
        self._turn_agent_message: dict[str, str | None] = {}
        self.refresh()

    def _load_prefs(self) -> dict[str, Any]:
        try:
            with open(self._prefs_path, encoding="utf-8") as handle:
                value = json.load(handle)
            return value if isinstance(value, dict) else {}
        except (OSError, ValueError):
            return {}

    def save(self) -> None:
        """Persist local-only desktop preferences, never the shared tree."""
        os.makedirs(self.home, exist_ok=True)
        payload = {
            "ui": self.data.get("ui") or {},
            "agents": self.data.get("agents") or {},
            "execution": self._execution,
        }
        temporary = self._prefs_path + ".tmp"
        encoded = json.dumps(payload, ensure_ascii=False, indent=2)
        with open(temporary, "w", encoding="utf-8") as handle:
            handle.write(encoded)
        for attempt in range(self.REPLACE_ATTEMPTS):
            try:
                os.replace(temporary, self._prefs_path)
                return
            except PermissionError:
                time.sleep(self.REPLACE_BACKOFF * (attempt + 1))
        # Windows scanners and sync clients can hold desktop.json open.  A
        # brief in-place write preserves the preference rather than surfacing a
        # false save failure; the completed temporary file remains available if
        # this final write is also interrupted.
        with open(self._prefs_path, "w", encoding="utf-8") as handle:
            handle.write(encoded)

    def touch(self) -> None:
        # The historical UI batches these calls.  There is no shared mutable
        # document to flush in remote mode, so saving the tiny preference file
        # is sufficient and intentionally harmless.
        pass

    def flush(self) -> bool:
        self.save()
        return True

    @property
    def projects(self) -> list[dict[str, Any]]:
        return self.data["projects"]

    @property
    def defaults(self) -> dict[str, Any]:
        return self.data["defaults"]

    @property
    def agents(self) -> dict[str, Any]:
        return self.data.setdefault("agents", {})

    def refresh(self) -> None:
        tree = self.client.tree()
        self.data["defaults"] = dict(tree.get("defaults") or {})
        if self._execution.get("workspace_cwd"):
            self.data["defaults"]["cwd"] = self._execution["workspace_cwd"]
        if self._execution.get("workspace_agent") in (store.CODEX_AGENT, store.CLAUDE_AGENT):
            self.data["defaults"]["agent_id"] = self._execution["workspace_agent"]
        self.data["projects"] = [self._normalise_project(project, None) for project in tree["projects"]]

    def _normalise_project(self, raw: dict[str, Any], parent: dict[str, Any] | None) -> dict[str, Any]:
        node = dict(raw)
        local = self._project_execution(node["id"])
        # CLI runners operate on this computer, so these choices intentionally
        # live in desktop.json rather than in the shared service record.
        if "cwd" in local:
            node["cwd"] = local["cwd"]
        if "default_agent" in local:
            node["default_agent"] = local["default_agent"]
        else:
            node["default_agent"] = None
        node["kind"] = store.PROJECT
        node["children"] = []
        self._parents[node["id"]] = parent
        for child in raw.get("children") or []:
            node["children"].append(self._normalise_project(child, node))
        for conversation in raw.get("conversations") or []:
            node["children"].append(self._normalise_conversation(conversation, node))
        return node

    def _normalise_conversation(self, raw: dict[str, Any], parent: dict[str, Any]) -> dict[str, Any]:
        node = dict(raw)
        node.update({
            "kind": store.CONVERSATION,
            "thread_id": raw.get("codex_thread_id"),
            "fork_of": raw.get("forked_from_external_session_id"),
            "fork_of_name": None,
            "messages": [],
        })
        self._parents[node["id"]] = parent
        self._load_messages(node)
        return node

    def _load_messages(self, conversation: dict[str, Any]) -> None:
        messages = list(self.client.iter_messages(conversation["id"]))
        for message in messages:
            for attachment in message.get("attachments") or []:
                attachment["local_path"] = self._cache_attachment(attachment)
        self._message_ids[conversation["id"]] = [message["id"] for message in messages]
        conversation["messages"] = [self._normalise_message(message) for message in messages]

    def _cache_attachment(self, attachment: dict[str, Any]) -> str | None:
        attachment_id = attachment.get("id")
        if not attachment_id:
            return None
        safe_name = os.path.basename(attachment.get("file_name") or attachment_id)
        destination = os.path.join(self.home, "attachment-cache", f"{attachment_id}-{safe_name}")
        if not os.path.isfile(destination):
            self.client.download_attachment(attachment_id, destination)
        return destination

    @staticmethod
    def _normalise_message(message: dict[str, Any]) -> dict[str, Any]:
        result = dict(message)
        result["text"] = result.pop("content", "")
        result["ts"] = result.get("created_at")
        # Existing rendering knows ``agent_tool`` and has no separate tool-call
        # object. Keep server tool calls visible without duplicating the answer.
        result["images"] = [attachment.get("local_path") for attachment in result.get("attachments", [])
                            if attachment.get("local_path")]
        return result

    def walk(self) -> Iterator[tuple[dict[str, Any], dict[str, Any] | None]]:
        def descend(nodes: list[dict[str, Any]], parent: dict[str, Any] | None):
            for node in nodes:
                yield node, parent
                if node["kind"] == store.PROJECT:
                    yield from descend(node["children"], node)
        yield from descend(self.projects, None)

    def find(self, node_id: str | None) -> dict[str, Any] | None:
        if not node_id:
            return None
        for node, _parent in self.walk():
            if node["id"] == node_id:
                return node
        return None

    def parent_of(self, node_id: str) -> dict[str, Any] | None:
        return self._parents.get(node_id)

    def siblings_of(self, node_id: str) -> list[dict[str, Any]]:
        parent = self.parent_of(node_id)
        return parent["children"] if parent else self.projects

    def ancestors(self, node_id: str) -> list[dict[str, Any]]:
        result = []
        parent = self.parent_of(node_id)
        while parent is not None:
            result.append(parent)
            parent = self.parent_of(parent["id"])
        return result

    def owning_project(self, node_id: str | None) -> dict[str, Any] | None:
        node = self.find(node_id)
        return node if node and node["kind"] == store.PROJECT else (self.parent_of(node_id) if node else None)

    def path_of(self, node_id: str) -> str:
        return self.client.path_of(node_id)

    def resolve(self, node_id: str) -> dict[str, Any]:
        resolved = self.client.settings_for(node_id)
        node = self.find(node_id)
        chain = ([node] if node else []) + self.ancestors(node_id)
        for candidate in chain:
            if candidate and candidate.get("kind") == store.PROJECT and candidate.get("cwd"):
                resolved["cwd"] = candidate["cwd"]
                break
        workspace_cwd = self._execution.get("workspace_cwd")
        if not any(candidate and candidate.get("kind") == store.PROJECT and candidate.get("cwd") for candidate in chain):
            if isinstance(workspace_cwd, str) and workspace_cwd:
                resolved["cwd"] = workspace_cwd
        return resolved

    def instructions_for(self, node_id: str, include_self: bool = True) -> str:
        return self.client.instructions_for(node_id) if include_self else ""

    def usage_of(self, node_id: str) -> dict[str, int]:
        return self.client.usage_of(node_id)

    def inherited(self, node_id: str, key: str) -> Any:
        parent = self.parent_of(node_id)
        return self.resolve(parent["id"] if parent else node_id).get(key)

    def unique_name(self, parent: dict[str, Any], base: str) -> str:
        names = {child["name"] for child in parent["children"]}
        candidate, number = base, 2
        while candidate in names:
            candidate = f"{base} {number}"
            number += 1
        return candidate

    def add_project(self, parent_id: str | None, name: str) -> dict[str, Any]:
        created = self.client.create_project(parent_id=parent_id, name=name)
        self.refresh()
        return self.find(created["id"]) or created

    def add_conversation(self, project_id: str, name: str) -> dict[str, Any]:
        # The server retains an effective runner for compatibility with older
        # clients; this desktop records that a new conversation inherits.
        created = self.client.create_conversation(
            project_id=project_id, name=name,
            agent_id=self.project_agent(project_id),
        )
        self._conversation_agents()[created["id"]] = None
        # The server has already created the conversation.  Losing access to
        # this machine's optional desktop preferences must not make the UI
        # look as if the button did nothing.
        try:
            self.save()
        except OSError as exc:
            self.last_local_save_error = exc
        else:
            self.last_local_save_error = None
        self.refresh()
        return self.find(created["id"]) or created

    def rename(self, node_id: str, name: str) -> None:
        node = self.find(node_id)
        if not node:
            return
        if node["kind"] == store.PROJECT:
            updated = self.client.update_project(node_id, revision=node["revision"], name=name)
        else:
            updated = self.client.update_conversation(node_id, revision=node["revision"], name=name)
        node.update(updated)

    def delete(self, node_id: str) -> None:
        node = self.find(node_id)
        if not node:
            return
        if node["kind"] == store.PROJECT:
            self.client.delete_project(node_id, revision=node["revision"])
        else:
            self.client.delete_conversation(node_id, revision=node["revision"])
        self.refresh()

    def move(self, node_id: str, new_parent_id: str | None, index: int | None = None) -> bool:
        node = self.find(node_id)
        if not node:
            return False
        if node["kind"] == store.PROJECT:
            self.client.move_project(node_id, revision=node["revision"], parent_id=new_parent_id, index=index)
        elif new_parent_id:
            self.client.move_conversation(node_id, revision=node["revision"], project_id=new_parent_id, index=index)
        else:
            return False
        self.refresh()
        return True

    def set_option(self, node_id: str, key: str, value: Any) -> None:
        node = self.find(node_id)
        if not node or node["kind"] != store.PROJECT:
            return
        if key in ("cwd", "default_agent"):
            local = self._project_execution(node_id)
            if value:
                local[key] = value
            else:
                local.pop(key, None)
            self.save()
            node[key] = value or None
            return
        updated = self.client.update_project(node_id, revision=node["revision"], **{key: value or None})
        node.update(updated)

    def set_expanded(self, node_id: str, expanded: bool) -> None:
        node = self.find(node_id)
        if node:
            self.client.set_expanded(node_id, expanded)
            node["expanded"] = expanded

    def conversation_agent(self, conv_id: str) -> str:
        node = self.find(conv_id)
        overrides = self._conversation_agents()
        if conv_id in overrides:
            selected = overrides[conv_id]
            if selected in (store.CODEX_AGENT, store.CLAUDE_AGENT):
                return selected
            return self.project_agent(conv_id)
        # Existing conversations keep their former explicit server choice.
        return (node or {}).get("agent_id") or store.DEFAULT_AGENT

    def conversation_agent_source(self, conv_id: str) -> str:
        if self._conversation_agents().get(conv_id) in (store.CODEX_AGENT, store.CLAUDE_AGENT):
            return "對話"
        if conv_id not in self._conversation_agents():
            return "對話"
        project = self.owning_project(conv_id)
        for candidate in ([project] if project else []) + self.ancestors(project["id"] if project else conv_id):
            if candidate and candidate.get("default_agent") in (store.CODEX_AGENT, store.CLAUDE_AGENT):
                return "專案"
        return "工作區" if self._execution.get("workspace_agent") in (store.CODEX_AGENT, store.CLAUDE_AGENT) else "Codex CLI"

    def project_agent(self, node_id: str) -> str:
        project = self.owning_project(node_id)
        chain = ([project] if project else []) + self.ancestors(project["id"] if project else node_id)
        for candidate in chain:
            if candidate and candidate.get("default_agent") in (store.CODEX_AGENT, store.CLAUDE_AGENT):
                return candidate["default_agent"]
        selected = self._execution.get("workspace_agent")
        return selected if selected in (store.CODEX_AGENT, store.CLAUDE_AGENT) else store.DEFAULT_AGENT

    def set_conversation_agent(self, conv_id: str, agent_id: str | None) -> None:
        node = self.find(conv_id)
        if node and (agent_id is None or agent_id in (store.CODEX_AGENT, store.CLAUDE_AGENT)):
            self._conversation_agents()[conv_id] = agent_id
            self.save()

    def set_workspace_default(self, key: str, value: Any) -> None:
        if key == "cwd":
            key = "workspace_cwd"
        if key not in ("workspace_cwd", "workspace_agent"):
            raise ValueError("未知的本機工作區設定")
        if value:
            self._execution[key] = value
        else:
            self._execution.pop(key, None)
        self.save()

    def _project_execution(self, project_id: str) -> dict[str, Any]:
        projects = self._execution.setdefault("projects", {})
        return projects.setdefault(project_id, {})

    def _conversation_agents(self) -> dict[str, Any]:
        return self._execution.setdefault("conversation_agents", {})

    def start_execution_record(self, conv_id: str, record: dict[str, Any]) -> str:
        record = dict(record)
        record_id = str(record.get("id") or new_key())
        record["id"] = record_id
        record.setdefault("tools", [])
        self._execution.setdefault("records", {}).setdefault(conv_id, []).append(record)
        self.save()
        return record_id

    def execution_records(self, conv_id: str) -> list[dict[str, Any]]:
        records = self._execution.get("records", {}).get(conv_id, [])
        return [dict(record) for record in records if isinstance(record, dict)]

    def update_execution_record(self, conv_id: str, record_id: str, **fields: Any) -> None:
        for record in self._execution.get("records", {}).get(conv_id, []):
            if record.get("id") == record_id:
                record.update(fields)
                self.save()
                return

    def add_execution_tool(self, conv_id: str, record_id: str, summary: str) -> None:
        for record in self._execution.get("records", {}).get(conv_id, []):
            if record.get("id") == record_id:
                record.setdefault("tools", []).append(summary)
                self.save()
                return

    def agent_path(self, agent_id: str) -> str | None:
        value = (self.agents.get(agent_id) or {}).get("path")
        return value.strip() if isinstance(value, str) and value.strip() else None

    def set_agent_path(self, agent_id: str, path: str) -> None:
        self.agents.setdefault(agent_id, {})["path"] = path.strip() or None
        self.save()

    def append_message(self, conv_id: str, role: str, text: str, **extra: Any) -> dict[str, Any] | None:
        node = self.find(conv_id)
        if not node:
            return None
        created = self.client.append_message(
            conv_id, role=role, content=text, agent_id=extra.get("agent_id"),
            metadata={key: value for key, value in extra.items() if key not in {"agent_id", "images"}},
            completed=True,
        )
        normalised = self._normalise_message(created)
        normalised["images"] = list(extra.get("images") or [])
        node["messages"].append(normalised)
        self._message_ids.setdefault(conv_id, []).append(created["id"])
        if role == "user":
            self._turn_agent_message[conv_id] = None
        elif role == "agent":
            self._turn_agent_message[conv_id] = created["id"]
        for path in extra.get("images") or []:
            if os.path.isfile(path):
                self.client.upload_file(conversation_id=conv_id, path=path, message_id=created["id"])
        return normalised

    def add_usage(self, conv_id: str, usage: dict[str, Any]) -> dict[str, int]:
        node = self.find(conv_id)
        if not node:
            return {}
        # Usage belongs to the final agent message for this turn.  A tool-only
        # failure still gets a completed system message so totals remain exact.
        target_id = self._turn_agent_message.get(conv_id)
        target = next((message for message in reversed(node["messages"])
                       if message.get("id") == target_id), None)
        if target is None:
            target = self.append_message(conv_id, "notice", "", completed=True)
        if target and target.get("id"):
            self.client.complete_message(target["id"], usage=usage)
        return self.client.usage_of(conv_id)

    def set_thread_id(self, conv_id: str, thread_id: str) -> None:
        node = self.find(conv_id)
        if node:
            updated = self.client.set_runner_state(conv_id, codex_thread_id=thread_id,
                                                   claude_session_id=node.get("claude_session_id"))
            node.update(updated)
            node["thread_id"] = thread_id

    def set_claude_session_id(self, conv_id: str, session_id: str) -> None:
        node = self.find(conv_id)
        if node:
            updated = self.client.set_runner_state(conv_id, codex_thread_id=node.get("thread_id"),
                                                   claude_session_id=session_id)
            node.update(updated)
            node["claude_session_id"] = session_id

    def clear_thread(self, conv_id: str) -> None:
        self.client.reset_conversation(conv_id)
        self.refresh()

    def fork_conversation(self, conv_id: str) -> dict[str, Any] | None:
        created = self.client.fork_conversation(conv_id)
        self.refresh()
        return self.find(created["id"])

def new_key() -> str:
    """One key per logical operation — reused across retries, never across calls."""
    return uuid.uuid4().hex


def _to_api_error(exc: urllib.error.HTTPError) -> ApiError:
    try:
        payload = json.loads(exc.read().decode("utf-8"))
    except Exception:  # noqa: BLE001 - a proxy or a crash can answer with anything
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    return ApiError(
        exc.code,
        payload.get("error") or "http_error",
        payload.get("detail") or exc.reason or "請求失敗",
        payload,
    )
