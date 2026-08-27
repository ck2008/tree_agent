"""One-shot import of a desktop `workspace.json` into the shared database.

Only an administrator can start it, and only by naming the file explicitly —
nothing here runs at startup, and nothing overwrites what is already in the
database. The source files are copied to a read-only backup first and are never
modified or deleted.

The import is a single transaction: either the whole workspace lands or none of
it does. Problems that affect one row and not the structure — a missing image
file, an unreadable attachment, a role this version has never heard of — are
recorded as issues in `migration_reports` and do not stop the rest.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import stat
from datetime import datetime
from typing import Any

from .. import ids
from ..errors import ConflictError, NotFound, ValidationError
from ..repositories import attachments as attachments_repo
from ..repositories import conversations as conversations_repo
from ..repositories import messages as messages_repo
from ..repositories import projects as projects_repo
from ..repositories import search as search_repo
from ..services.access import require_admin
from ..services.attachments import safe_file_name, sniff_mime

KIND_PROJECT = "project"
KIND_CONVERSATION = "conversation"

# Legacy roles that this schema names differently. `agent_tool` was Claude's
# terse per-tool-call event; it is a tool message with a marker, not an unknown
# role to be demoted to a notice.
ROLE_MAP = {"agent_tool": ("tool", "agent_tool")}
KNOWN_ROLES = set(messages_repo.ROLES)

MAX_ATTACHMENT_BYTES = attachments_repo.MAX_BYTES
NAME_MAX = 200
CHUNK_SIZE = attachments_repo.CHUNK_SIZE
SAMPLE_VERIFY = 8  # attachments re-hashed from the database after the commit


class ImportIssue(dict):
    def __init__(self, kind: str, detail: str, **extra: Any) -> None:
        super().__init__(kind=kind, detail=detail, **extra)


def import_workspace(
    services: Any,
    actor: Any,
    *,
    source_path: str,
    parent_project_id: str | None = None,
    dry_run: bool = False,
    backup: bool = True,
) -> dict[str, Any]:
    """Import `source_path` (a `workspace.json`) under an optional parent project."""
    require_admin(actor)
    source_path = os.path.abspath(source_path)
    if not os.path.isfile(source_path):
        raise NotFound(f"找不到 workspace.json：{source_path}")

    document = _load(source_path)
    nodes = document.get("projects") or []
    if not isinstance(nodes, list) or not nodes:
        raise ValidationError("這個 workspace.json 裡沒有任何專案")
    _reject_conversations_with_children(nodes)

    home = os.path.dirname(source_path)
    issues: list[dict[str, Any]] = []
    counts = _count(nodes)

    if dry_run:
        return {
            "status": "dry_run",
            "source_path": source_path,
            "summary": counts,
            "issues": _preflight(nodes, home),
        }

    backup_path = _backup(source_path, home) if backup else None
    started_at = ids.now_ms()

    def job(conn: sqlite3.Connection) -> dict[str, Any]:
        if parent_project_id is not None:
            if projects_repo.get(conn, parent_project_id) is None:
                raise NotFound("找不到要匯入到的專案")
        state = _State(conn, actor, issues, home)
        state.import_level(nodes, parent_project_id)
        return state.summary()

    try:
        summary = services.db.write(job, label="legacy_import")
    except Exception as exc:  # noqa: BLE001 - recorded, then re-raised
        _write_report(
            services,
            actor,
            source_path=source_path,
            started_at=started_at,
            status="failed",
            summary={"source_counts": counts},
            issues=[*issues, ImportIssue("fatal", str(exc))],
        )
        raise

    summary["source_counts"] = counts
    summary["backup_path"] = backup_path
    verified = _verify(services, summary, issues)
    summary["verified_attachments"] = verified

    report_id = _write_report(
        services,
        actor,
        source_path=source_path,
        started_at=started_at,
        status="completed",
        summary=summary,
        issues=issues,
    )
    return {
        "status": "completed",
        "report_id": report_id,
        "source_path": source_path,
        "summary": summary,
        "issues": issues,
    }


# ------------------------------------------------------------------ reading


def _load(path: str) -> dict[str, Any]:
    try:
        with open(path, encoding="utf-8") as handle:
            document = json.load(handle)
    except (OSError, json.JSONDecodeError) as exc:
        raise ValidationError(f"無法讀取 workspace.json：{exc}") from exc
    if not isinstance(document, dict):
        raise ValidationError("workspace.json 的最外層必須是物件")
    return document


def _reject_conversations_with_children(nodes: list[dict[str, Any]]) -> None:
    """A conversation with children breaks the "conversations are leaves" rule.

    That is a structural problem, not a per-row one: importing it would silently
    lose data, so the whole file is refused instead.
    """

    def walk(items: list[dict[str, Any]], path: str) -> None:
        for node in items:
            name = node.get("name") or "?"
            here = f"{path} / {name}" if path else name
            if node.get("kind") == KIND_CONVERSATION and node.get("children"):
                raise ConflictError(f"「{here}」是對話卻含有子節點，無法匯入")
            walk(node.get("children") or [], here)

    walk(nodes, "")


def _count(nodes: list[dict[str, Any]]) -> dict[str, int]:
    counts = {"projects": 0, "conversations": 0, "messages": 0, "image_references": 0}

    def walk(items: list[dict[str, Any]]) -> None:
        for node in items:
            if node.get("kind") == KIND_CONVERSATION:
                counts["conversations"] += 1
                for message in node.get("messages") or []:
                    counts["messages"] += 1
                    counts["image_references"] += len(message.get("images") or [])
            else:
                counts["projects"] += 1
                walk(node.get("children") or [])

    walk(nodes)
    return counts


def _preflight(nodes: list[dict[str, Any]], home: str) -> list[dict[str, Any]]:
    """What a real run would complain about, without writing anything."""
    issues: list[dict[str, Any]] = []
    seen: set[str] = set()

    def walk(items: list[dict[str, Any]]) -> None:
        for node in items:
            if node.get("kind") == KIND_CONVERSATION:
                for message in node.get("messages") or []:
                    role = message.get("role")
                    if role not in KNOWN_ROLES and role not in ROLE_MAP:
                        issues.append(ImportIssue("unknown_role", f"角色 {role!r} 會轉為 notice"))
                    for image in message.get("images") or []:
                        resolved = _resolve_attachment(image, home)
                        if resolved in seen:
                            continue
                        seen.add(resolved)
                        if not os.path.isfile(resolved):
                            issues.append(ImportIssue("missing_attachment", resolved))
                        elif os.path.getsize(resolved) > MAX_ATTACHMENT_BYTES:
                            issues.append(ImportIssue("attachment_too_large", resolved))
            else:
                walk(node.get("children") or [])

    walk(nodes)
    return issues


def _backup(source_path: str, home: str) -> str:
    """A read-only copy of the source, so the import can never be the only copy."""
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    target_dir = os.path.join(home, f"import-backup-{stamp}")
    os.makedirs(target_dir, exist_ok=True)
    copied = shutil.copy2(source_path, os.path.join(target_dir, "workspace.json"))
    os.chmod(copied, stat.S_IREAD)
    attachments_dir = os.path.join(home, "attachments")
    if os.path.isdir(attachments_dir):
        destination = os.path.join(target_dir, "attachments")
        shutil.copytree(attachments_dir, destination, dirs_exist_ok=True)
        for root, _, files in os.walk(destination):
            for name in files:
                os.chmod(os.path.join(root, name), stat.S_IREAD)
    return target_dir


def _resolve_attachment(reference: str, home: str) -> str:
    reference = reference or ""
    if os.path.isabs(reference):
        return reference
    return os.path.abspath(os.path.join(home, reference))


# ------------------------------------------------------------------ writing


class _State:
    """Carries the open transaction and the running tallies through the walk."""

    def __init__(
        self,
        conn: sqlite3.Connection,
        actor: Any,
        issues: list[dict[str, Any]],
        home: str,
    ) -> None:
        self.conn = conn
        self.actor = actor
        self.issues = issues
        self.home = home
        self.now = ids.now_ms()
        self.counts = {
            "projects": 0,
            "conversations": 0,
            "messages": 0,
            "attachments": 0,
            "deduplicated_attachments": 0,
        }
        # Legacy image path -> attachment id, so one file on disk becomes one
        # row however many messages point at it.
        self.attachment_cache: dict[str, str] = {}
        self.imported_attachment_ids: list[str] = []

    # -------------------------------------------------------------- nodes

    def import_level(self, items: list[dict[str, Any]], parent_id: str | None) -> None:
        """One level of siblings, keeping the order the JSON file had."""
        keys = ids.resequence(len(items))
        for node, sort_key in zip(items, keys):
            if node.get("kind") == KIND_CONVERSATION:
                target = parent_id
                if target is None:
                    # A top-level conversation has nowhere to live; give it one
                    # rather than dropping it.
                    target = self._create_project({"name": "匯入的對話"}, None, sort_key)
                    self.issues.append(
                        ImportIssue(
                            "wrapped_conversation",
                            f"「{node.get('name')}」放進了新的「匯入的對話」專案",
                        )
                    )
                self._create_conversation(node, target, sort_key)
                continue
            project_id = self._create_project(node, parent_id, sort_key)
            self.import_level(node.get("children") or [], project_id)

    def _create_project(self, node: dict[str, Any], parent_id: str | None, sort_key: str) -> str:
        name = self._unique_project_name(parent_id, node.get("name"))
        settings = {key: node.get(key) or None for key in projects_repo.SETTING_KEYS}
        created = _timestamp(node.get("created_at"), self.now)
        project_id = projects_repo.insert(
            self.conn,
            parent_id=parent_id,
            name=name,
            # Sibling order in the JSON is the only ordering the old format had.
            sort_key=sort_key,
            created_by=self.actor.id,
            now_ms=created,
            settings=settings,
        )
        if not node.get("expanded", True):
            projects_repo.set_expanded(self.conn, project_id, False)
        # Whoever ran the import owns everything it created; they can hand out
        # narrower grants afterwards.
        projects_repo.grant(
            self.conn,
            project_id=project_id,
            user_id=self.actor.id,
            permission="owner",
            granted_by=self.actor.id,
            now_ms=self.now,
        )
        search_repo.index_project(self.conn, project_id, name, settings.get("prompt"))
        self.counts["projects"] += 1
        return project_id

    def _create_conversation(self, node: dict[str, Any], project_id: str, sort_key: str) -> str:
        name = self._unique_conversation_name(project_id, node.get("name"))
        agent_id = node.get("agent_id")
        if agent_id not in conversations_repo.AGENTS:
            if agent_id:
                self.issues.append(
                    ImportIssue("unknown_agent", f"「{name}」的 agent {agent_id!r} 改為 codex")
                )
            agent_id = conversations_repo.DEFAULT_AGENT
        created = _timestamp(node.get("created_at"), self.now)
        updated = _timestamp(node.get("updated_at"), created)

        conversation_id = conversations_repo.insert(
            self.conn,
            project_id=project_id,
            name=name,
            sort_key=sort_key,
            agent_id=agent_id,
            model=None,
            created_by=self.actor.id,
            now_ms=created,
            forked_from_external_session_id=node.get("fork_of"),
        )
        conversations_repo.update(
            self.conn,
            conversation_id,
            {
                "codex_thread_id": node.get("thread_id"),
                "claude_session_id": node.get("claude_session_id"),
            },
            expected_revision=None,
            now_ms=updated,
        )
        search_repo.index_conversation(self.conn, conversation_id, project_id, name)
        self.counts["conversations"] += 1

        for sequence, message in enumerate(node.get("messages") or [], start=1):
            self._create_message(message, conversation_id, project_id, sequence)
        return conversation_id

    def _create_message(
        self, message: dict[str, Any], conversation_id: str, project_id: str, sequence: int
    ) -> None:
        raw_role = message.get("role")
        metadata = {
            key: value
            for key, value in message.items()
            if key not in ("role", "text", "ts", "images", "agent_id")
        }
        if raw_role in ROLE_MAP:
            role, channel = ROLE_MAP[raw_role]
            metadata["channel"] = channel
        elif raw_role in KNOWN_ROLES:
            role = raw_role
        else:
            role = "notice"
            metadata["legacy_role"] = raw_role
            self.issues.append(
                ImportIssue("unknown_role", f"角色 {raw_role!r} 轉為 notice", conversation_id=conversation_id)
            )

        created = _timestamp(message.get("ts"), self.now)
        content = message.get("text") or ""
        message_id, _ = messages_repo.insert(
            self.conn,
            conversation_id=conversation_id,
            role=role,
            content=content,
            sequence_no=sequence,
            agent_id=message.get("agent_id"),
            metadata=metadata,
            created_by=self.actor.id,
            now_ms=created,
            completed_at_ms=created,
        )
        self.counts["messages"] += 1
        if role in ("user", "agent", "reasoning", "error", "notice", "meta"):
            search_repo.index_message(self.conn, message_id, conversation_id, project_id, content)

        for position, reference in enumerate(message.get("images") or []):
            attachment_id = self._import_attachment(reference)
            if attachment_id is not None:
                attachments_repo.link(self.conn, message_id, attachment_id, position)

    # -------------------------------------------------------- attachments

    def _import_attachment(self, reference: str) -> str | None:
        path = _resolve_attachment(reference, self.home)
        if path in self.attachment_cache:
            return self.attachment_cache[path]
        try:
            size = os.path.getsize(path)
        except OSError:
            self.issues.append(ImportIssue("missing_attachment", reference))
            return None
        if size == 0 or size > MAX_ATTACHMENT_BYTES:
            self.issues.append(
                ImportIssue("attachment_size_rejected", reference, byte_size=size)
            )
            return None
        try:
            upload_id, digest, chunk_count = self._stage_attachment(path, size)
        except OSError as exc:
            self.issues.append(ImportIssue("unreadable_attachment", f"{reference}: {exc}"))
            return None

        existing = attachments_repo.by_hash(self.conn, digest, size)
        if existing is not None:
            self.counts["deduplicated_attachments"] += 1
            self.attachment_cache[path] = existing["id"]
            return existing["id"]

        # The file was staged a 1 MiB block at a time.  Promote those rows
        # inside this same import transaction, then immediately drop staging;
        # unlike the old list-of-chunks implementation this never retains a
        # whole 20 MiB file in Python memory.
        attachment_id = attachments_repo.create_from_upload(
            self.conn,
            upload_id=upload_id,
            sha256=digest,
            file_name=safe_file_name(os.path.basename(path)),
            mime_type=sniff_mime(path),
            byte_size=size,
            chunk_count=chunk_count,
            created_by=self.actor.id,
            now_ms=self.now,
        )
        attachments_repo.drop_upload_chunks(self.conn, upload_id)
        attachments_repo.set_upload_status(
            self.conn,
            upload_id,
            "committed",
            committed_attachment_id=attachment_id,
        )
        self.attachment_cache[path] = attachment_id
        self.imported_attachment_ids.append(attachment_id)
        self.counts["attachments"] += 1
        return attachment_id

    def _stage_attachment(self, path: str, size: int) -> tuple[str, str, int]:
        """Hash and stage an import attachment in bounded-size SQLite rows."""
        upload_id = attachments_repo.create_upload(
            self.conn,
            user_id=self.actor.id,
            file_name=safe_file_name(os.path.basename(path)),
            mime_type=sniff_mime(path),
            expected_byte_size=size,
            expected_sha256=None,
            target_message_id=None,
            now_ms=self.now,
        )
        digest = hashlib.sha256()
        chunk_count = 0
        with open(path, "rb") as handle:
            while block := handle.read(CHUNK_SIZE):
                digest.update(block)
                attachments_repo.put_upload_chunk(
                    self.conn, upload_id, chunk_count, block
                )
                chunk_count += 1
        return upload_id, digest.hexdigest(), chunk_count

    def summary(self) -> dict[str, Any]:
        return {**self.counts, "attachment_ids": self.imported_attachment_ids[:SAMPLE_VERIFY]}

    # ------------------------------------------------------------- naming

    def _unique_project_name(self, parent_id: str | None, name: str | None) -> str:
        base = (name or "未命名專案").strip()[:NAME_MAX] or "未命名專案"
        candidate, counter = base, 2
        while projects_repo.live_name_taken(self.conn, parent_id, candidate):
            candidate = f"{base} ({counter})"
            counter += 1
        if candidate != base:
            self.issues.append(ImportIssue("renamed", f"「{base}」與既有項目同名，改為「{candidate}」"))
        return candidate

    def _unique_conversation_name(self, project_id: str, name: str | None) -> str:
        base = (name or "新對話").strip()[:NAME_MAX] or "新對話"
        candidate, counter = base, 2
        while conversations_repo.live_name_taken(self.conn, project_id, candidate):
            candidate = f"{base} ({counter})"
            counter += 1
        if candidate != base:
            self.issues.append(ImportIssue("renamed", f"「{base}」與既有對話同名，改為「{candidate}」"))
        return candidate


def _timestamp(value: Any, fallback: int) -> int:
    """Legacy timestamps are local ISO strings; anything unparseable falls back."""
    if isinstance(value, (int, float)) and value > 0:
        return int(value if value > 1e11 else value * 1000)
    if isinstance(value, str) and value.strip():
        try:
            parsed = datetime.fromisoformat(value.strip())
        except ValueError:
            return fallback
        if parsed.tzinfo is None:
            parsed = parsed.astimezone()
        return int(parsed.timestamp() * 1000)
    return fallback


def _verify(services: Any, summary: dict[str, Any], issues: list[dict[str, Any]]) -> int:
    """Re-hash a sample of attachments straight out of the database."""
    verified = 0
    for attachment_id in summary.get("attachment_ids") or []:
        with services.db.read() as conn:
            row = attachments_repo.get(conn, attachment_id)
            if row is None:
                issues.append(ImportIssue("verify_missing", attachment_id))
                continue
            digest = hashlib.sha256()
            total = 0
            for chunk in attachments_repo.iter_chunks(conn, attachment_id):
                digest.update(chunk)
                total += len(chunk)
        if digest.hexdigest() != row["sha256"] or total != row["byte_size"]:
            issues.append(ImportIssue("verify_mismatch", attachment_id))
        else:
            verified += 1
    return verified


def _write_report(
    services: Any,
    actor: Any,
    *,
    source_path: str,
    started_at: int,
    status: str,
    summary: dict[str, Any],
    issues: list[dict[str, Any]],
) -> str:
    report_id = ids.new_id()

    def job(conn: sqlite3.Connection) -> None:
        conn.execute(
            "INSERT INTO migration_reports (id, kind, source_path, started_at_ms,"
            " completed_at_ms, status, summary_json, issues_json, created_by)"
            " VALUES (?, 'legacy_workspace_json', ?, ?, ?, ?, ?, ?, ?)",
            (
                report_id,
                source_path,
                started_at,
                ids.now_ms(),
                status,
                json.dumps(summary, ensure_ascii=False, default=str),
                json.dumps(issues, ensure_ascii=False, default=str),
                actor.id,
            ),
        )

    services.db.write(job, label="migration_report")
    return report_id
