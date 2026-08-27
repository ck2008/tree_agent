"""SQL for conversations — always a leaf, always inside exactly one project."""

from __future__ import annotations

import sqlite3
from typing import Any

from .. import ids

AGENTS = ("codex", "claude")
DEFAULT_AGENT = "codex"

COLUMNS = (
    "id, project_id, name, sort_key, agent_id, model, codex_thread_id,"
    " claude_session_id, forked_from_conversation_id, forked_from_external_session_id,"
    " revision, created_by, created_at_ms, updated_at_ms, deleted_at_ms, deleted_by"
)

# Only these columns may be named by a caller-supplied `fields` mapping. The
# service layer already allowlists what a request may change; this is the
# backstop that keeps a future caller from turning a key into SQL.
UPDATABLE = frozenset(
    {"project_id", "name", "sort_key", "agent_id", "model", "codex_thread_id",
     "claude_session_id", "forked_from_conversation_id",
     "forked_from_external_session_id"}
)


def _assignments(fields: dict[str, Any]) -> str:
    unknown = set(fields) - UPDATABLE
    if unknown:
        raise ValueError(f"not updatable columns: {sorted(unknown)}")
    return ", ".join(f"{key} = ?" for key in fields)



def row_to_conversation(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    return {
        "id": row["id"],
        "project_id": row["project_id"],
        "name": row["name"],
        "sort_key": row["sort_key"],
        "agent_id": row["agent_id"],
        "model": row["model"],
        "codex_thread_id": row["codex_thread_id"],
        "claude_session_id": row["claude_session_id"],
        "forked_from_conversation_id": row["forked_from_conversation_id"],
        "forked_from_external_session_id": row["forked_from_external_session_id"],
        "revision": row["revision"],
        "created_by": row["created_by"],
        "created_at": ids.to_iso(row["created_at_ms"]),
        "updated_at": ids.to_iso(row["updated_at_ms"]),
        "deleted_at": ids.to_iso(row["deleted_at_ms"]),
    }


def get(
    conn: sqlite3.Connection, conversation_id: str, *, include_deleted: bool = False
) -> sqlite3.Row | None:
    clause = "" if include_deleted else " AND deleted_at_ms IS NULL"
    return conn.execute(
        f"SELECT {COLUMNS} FROM conversations WHERE id = ?{clause}", (conversation_id,)
    ).fetchone()


def list_for_project(conn: sqlite3.Connection, project_id: str) -> list[sqlite3.Row]:
    return conn.execute(
        f"SELECT {COLUMNS} FROM conversations WHERE project_id = ?"
        " AND deleted_at_ms IS NULL ORDER BY sort_key",
        (project_id,),
    ).fetchall()


def list_live(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        f"SELECT {COLUMNS} FROM conversations WHERE deleted_at_ms IS NULL"
        " ORDER BY project_id, sort_key"
    ).fetchall()


def sibling_sort_keys(conn: sqlite3.Connection, project_id: str) -> list[str]:
    return [row["sort_key"] for row in list_for_project(conn, project_id)]


def live_name_taken(
    conn: sqlite3.Connection, project_id: str, name: str, *, exclude_id: str | None = None
) -> bool:
    params: list[Any] = [project_id, name]
    exclusion = ""
    if exclude_id:
        exclusion = " AND id <> ?"
        params.append(exclude_id)
    return (
        conn.execute(
            "SELECT 1 FROM conversations WHERE project_id = ? AND name = ? COLLATE NOCASE"
            f" AND deleted_at_ms IS NULL{exclusion} LIMIT 1",
            params,
        ).fetchone()
        is not None
    )


def insert(
    conn: sqlite3.Connection,
    *,
    project_id: str,
    name: str,
    sort_key: str,
    agent_id: str,
    created_by: str,
    now_ms: int,
    model: str | None = None,
    forked_from_conversation_id: str | None = None,
    forked_from_external_session_id: str | None = None,
    conversation_id: str | None = None,
) -> str:
    conversation_id = conversation_id or ids.new_id()
    conn.execute(
        "INSERT INTO conversations (id, project_id, name, sort_key, agent_id, model,"
        " forked_from_conversation_id, forked_from_external_session_id, revision,"
        " created_by, created_at_ms, updated_at_ms)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?)",
        (
            conversation_id,
            project_id,
            name,
            sort_key,
            agent_id,
            model,
            forked_from_conversation_id,
            forked_from_external_session_id,
            created_by,
            now_ms,
            now_ms,
        ),
    )
    return conversation_id


def update(
    conn: sqlite3.Connection,
    conversation_id: str,
    fields: dict[str, Any],
    *,
    expected_revision: int | None,
    now_ms: int,
) -> int:
    assignments = _assignments(fields)
    if assignments:
        assignments += ", "
    params = list(fields.values()) + [now_ms, conversation_id]
    revision_clause = ""
    if expected_revision is not None:
        revision_clause = " AND revision = ?"
        params.append(expected_revision)
    return conn.execute(
        f"UPDATE conversations SET {assignments}revision = revision + 1,"
        f" updated_at_ms = ? WHERE id = ? AND deleted_at_ms IS NULL{revision_clause}",
        params,
    ).rowcount


def touch(conn: sqlite3.Connection, conversation_id: str, now_ms: int) -> None:
    """Bump `updated_at` without a revision: streamed messages are not edits."""
    conn.execute(
        "UPDATE conversations SET updated_at_ms = ? WHERE id = ?",
        (now_ms, conversation_id),
    )


def soft_delete(
    conn: sqlite3.Connection, conversation_id: str, *, deleted_by: str, now_ms: int
) -> list[str]:
    """Mark the conversation and its live messages. Returns the message ids."""
    message_ids = [
        row["id"]
        for row in conn.execute(
            "SELECT id FROM messages WHERE conversation_id = ? AND deleted_at_ms IS NULL",
            (conversation_id,),
        )
    ]
    conn.execute(
        "UPDATE conversations SET deleted_at_ms = ?, deleted_by = ?,"
        " revision = revision + 1 WHERE id = ? AND deleted_at_ms IS NULL",
        (now_ms, deleted_by, conversation_id),
    )
    if message_ids:
        marks = ",".join("?" * len(message_ids))
        conn.execute(
            f"UPDATE messages SET deleted_at_ms = ? WHERE id IN ({marks})",
            [now_ms, *message_ids],
        )
    return message_ids


def restore(conn: sqlite3.Connection, conversation_id: str, deleted_at_ms: int) -> list[str]:
    message_ids = [
        row["id"]
        for row in conn.execute(
            "SELECT id FROM messages WHERE conversation_id = ? AND deleted_at_ms = ?",
            (conversation_id, deleted_at_ms),
        )
    ]
    conn.execute(
        "UPDATE conversations SET deleted_at_ms = NULL, deleted_by = NULL,"
        " revision = revision + 1 WHERE id = ?",
        (conversation_id,),
    )
    if message_ids:
        marks = ",".join("?" * len(message_ids))
        conn.execute(
            f"UPDATE messages SET deleted_at_ms = NULL WHERE id IN ({marks})",
            message_ids,
        )
    return message_ids
