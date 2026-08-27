"""Retention sweeps, permanent purge and backups.

Nothing here runs on a timer by itself. `sweep()` is cheap and safe and the app
calls it periodically; `purge_deleted()` destroys data for good, so it is
admin-only, has a floor on the retention window, and writes an audit record
before it touches anything.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
from typing import Any

from .. import ids
from ..db import Database
from ..errors import ValidationError
from ..repositories import attachments as attachments_repo
from ..repositories import idempotency as idempotency_repo
from ..repositories import search as search_repo
from ..repositories import users as users_repo
from .access import Actor, require_admin

# The spec's retention floor. A shorter window would make "restore" a promise
# the service cannot keep.
MIN_RETENTION_DAYS = 30
DAY_MS = 24 * 60 * 60 * 1000

audit = logging.getLogger("tree_agent.audit")


class MaintenanceService:
    def __init__(self, db: Database) -> None:
        self.db = db

    def sweep(self) -> dict[str, int]:
        """Expire abandoned uploads, replay records and dead sessions."""

        def job(conn: sqlite3.Connection) -> dict[str, int]:
            now = ids.now_ms()
            return {
                "expired_uploads": attachments_repo.expire_stale_uploads(conn, now),
                "purged_upload_records": attachments_repo.purge_finished_uploads(
                    conn, now - 7 * DAY_MS
                ),
                "expired_idempotency_keys": idempotency_repo.purge_expired(conn, now),
                # Revoked sessions are kept for a while as a login audit trail;
                # only ones that expired long ago go.
                "purged_sessions": users_repo.purge_expired_sessions(conn, now - 7 * DAY_MS),
            }

        return self.db.write(job, label="sweep")

    def stats(self) -> dict[str, Any]:
        """Health numbers worth watching on the service host (spec §10)."""
        with self.db.read() as conn:
            counts = {
                name: conn.execute(f"SELECT count(*) FROM {name}").fetchone()[0]
                for name in (
                    "users",
                    "projects",
                    "conversations",
                    "messages",
                    "attachments",
                    "attachment_uploads",
                )
            }
            page_size = conn.execute("PRAGMA page_size").fetchone()[0]
            page_count = conn.execute("PRAGMA page_count").fetchone()[0]
            attachment_bytes = conn.execute(
                "SELECT COALESCE(SUM(byte_size), 0) FROM attachments WHERE deleted_at_ms IS NULL"
            ).fetchone()[0]
        return {
            "counts": counts,
            "database_bytes": page_size * page_count,
            "attachment_bytes": attachment_bytes,
            # A `-wal` that keeps growing means a reader is holding a snapshot
            # open and checkpointing cannot run — worth an alert, not just a log.
            "wal_bytes": _file_size(self.db.path + "-wal"),
            "shm_bytes": _file_size(self.db.path + "-shm"),
            "writer_queue_depth": self.db.writer_depth,
        }

    def purge_deleted(
        self, actor: Actor, *, retention_days: int = MIN_RETENTION_DAYS, dry_run: bool = False
    ) -> dict[str, Any]:
        """Destroy soft-deleted rows older than the retention window.

        Order is the reverse of the dependency graph, so no foreign key is ever
        left dangling mid-transaction: search rows, then tool calls, attachment
        links and messages, then conversations (after detaching forks that point
        at them), then memberships and projects deepest-first. Attachment bytes
        go last and only when nothing references them at all.
        """
        require_admin(actor)
        if retention_days < MIN_RETENTION_DAYS:
            raise ValidationError(f"保留期不得少於 {MIN_RETENTION_DAYS} 天")
        cutoff = ids.now_ms() - retention_days * DAY_MS

        def job(conn: sqlite3.Connection) -> dict[str, Any]:
            project_ids = [
                row["id"]
                for row in conn.execute(
                    "SELECT id FROM projects WHERE deleted_at_ms IS NOT NULL"
                    " AND deleted_at_ms <= ?",
                    (cutoff,),
                )
            ]
            conversation_ids = [
                row["id"]
                for row in conn.execute(
                    "SELECT id FROM conversations WHERE deleted_at_ms IS NOT NULL"
                    " AND deleted_at_ms <= ?",
                    (cutoff,),
                )
            ]
            message_ids = _message_ids_to_purge(conn, conversation_ids, cutoff)
            summary = {
                "projects": len(project_ids),
                "conversations": len(conversation_ids),
                "messages": len(message_ids),
                "attachments": 0,
                "retention_days": retention_days,
                "cutoff_ms": cutoff,
                "dry_run": dry_run,
            }
            if dry_run:
                candidates = _orphan_candidates(conn, message_ids)
                summary["attachments"] = len(candidates)
                return summary

            audit.warning(
                "permanent purge starting %s",
                json.dumps({"actor": actor.username, **summary}, ensure_ascii=False),
            )

            search_repo.drop_messages(conn, message_ids)
            search_repo.drop_conversations(conn, conversation_ids)
            search_repo.drop_projects(conn, project_ids)
            idempotency_repo.purge_expired(conn, ids.now_ms())

            candidates = _orphan_candidates(conn, message_ids)
            _delete_in(conn, "tool_calls", "message_id", message_ids)
            _delete_in(conn, "message_attachments", "message_id", message_ids)
            # Anything still pointing at a message has to let go of it first:
            # its own children, and any upload that was staged against it.
            _null_out(conn, "messages", "parent_message_id", message_ids)
            _null_out(conn, "attachment_uploads", "target_message_id", message_ids)
            _delete_in(conn, "messages", "id", message_ids)

            _detach_forks(conn, conversation_ids)
            _delete_in(conn, "conversations", "id", conversation_ids)

            _delete_in(conn, "project_memberships", "project_id", project_ids)
            for project_id in _deepest_first(conn, project_ids):
                conn.execute("DELETE FROM projects WHERE id = ?", (project_id,))

            purged_attachments = 0
            for attachment_id in candidates:
                if not attachments_repo.messages_referencing(conn, attachment_id):
                    attachments_repo.delete_bytes(conn, attachment_id)
                    purged_attachments += 1
            summary["attachments"] = purged_attachments

            audit.warning(
                "permanent purge finished %s",
                json.dumps({"actor": actor.username, **summary}, ensure_ascii=False),
            )
            return summary

        return self.db.write(job, label="purge_deleted")

    def backup(self, actor: Actor, destination: str) -> dict[str, Any]:
        require_admin(actor)
        path = self.db.backup(destination)
        audit.warning(
            "backup written %s",
            json.dumps({"actor": actor.username, "path": path}, ensure_ascii=False),
        )
        return {"path": path, "integrity": self.db.integrity_check()}


# ---------------------------------------------------------------- helpers


def _file_size(path: str) -> int:
    try:
        return os.path.getsize(path)
    except OSError:
        return 0


def _chunks(values: list[str], size: int = 400):
    for start in range(0, len(values), size):
        yield values[start : start + size]


def _delete_in(
    conn: sqlite3.Connection, table: str, column: str, values: list[str], *, null_out: bool = False
) -> int:
    total = 0
    for batch in _chunks(values):
        marks = ",".join("?" * len(batch))
        statement = (
            f"UPDATE {table} SET {column} = NULL WHERE {column} IN ({marks})"
            if null_out
            else f"DELETE FROM {table} WHERE {column} IN ({marks})"
        )
        total += conn.execute(statement, batch).rowcount
    return total


def _null_out(conn: sqlite3.Connection, table: str, column: str, values: list[str]) -> int:
    return _delete_in(conn, table, column, values, null_out=True)


def _detach_forks(conn: sqlite3.Connection, conversation_ids: list[str]) -> None:
    """Keep the provenance while dropping the foreign key about to disappear."""
    for batch in _chunks(conversation_ids):
        marks = ",".join("?" * len(batch))
        conn.execute(
            "UPDATE conversations SET"
            " forked_from_external_session_id ="
            "   COALESCE(forked_from_external_session_id, forked_from_conversation_id),"
            " forked_from_conversation_id = NULL"
            f" WHERE forked_from_conversation_id IN ({marks})",
            batch,
        )


def _message_ids_to_purge(
    conn: sqlite3.Connection, conversation_ids: list[str], cutoff: int
) -> list[str]:
    """Old deleted messages, plus everything inside a conversation that is going."""
    found = {
        row["id"]
        for row in conn.execute(
            "SELECT id FROM messages WHERE deleted_at_ms IS NOT NULL AND deleted_at_ms <= ?",
            (cutoff,),
        )
    }
    for batch in _chunks(conversation_ids):
        marks = ",".join("?" * len(batch))
        found.update(
            row["id"]
            for row in conn.execute(
                f"SELECT id FROM messages WHERE conversation_id IN ({marks})", batch
            )
        )
    return sorted(found)


def _orphan_candidates(conn: sqlite3.Connection, message_ids: list[str]) -> list[str]:
    """Attachments that would lose a reference — checked again after deletion."""
    found: set[str] = set()
    for batch in _chunks(message_ids):
        marks = ",".join("?" * len(batch))
        found.update(
            row["attachment_id"]
            for row in conn.execute(
                f"SELECT DISTINCT attachment_id FROM message_attachments"
                f" WHERE message_id IN ({marks})",
                batch,
            )
        )
    return sorted(found)


def _deepest_first(conn: sqlite3.Connection, project_ids: list[str]) -> list[str]:
    """Children before parents, so `projects.parent_id` never dangles."""
    depth: dict[str, int] = {}
    rows = {
        row["id"]: row["parent_id"]
        for row in conn.execute("SELECT id, parent_id FROM projects")
    }
    for project_id in project_ids:
        level, current, guard = 0, project_id, 0
        while current in rows and rows[current] and guard < 500:
            current = rows[current]
            level += 1
            guard += 1
        depth[project_id] = level
    return sorted(project_ids, key=lambda pid: -depth.get(pid, 0))
