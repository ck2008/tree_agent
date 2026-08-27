"""SQL for messages and the tool calls hanging off them."""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from .. import ids

ROLES = ("user", "agent", "reasoning", "tool", "error", "notice", "meta")
CONTENT_FORMATS = ("plain", "markdown", "json")
TOOL_STATUSES = ("pending", "running", "completed", "failed", "cancelled")

COLUMNS = (
    "id, conversation_id, parent_message_id, sequence_no, role, content,"
    " content_format, agent_id, model, external_event_id, metadata_json,"
    " created_by, created_at_ms, completed_at_ms, deleted_at_ms"
)


def row_to_message(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    try:
        metadata = json.loads(row["metadata_json"])
    except (TypeError, ValueError):
        metadata = {}
    return {
        "id": row["id"],
        "conversation_id": row["conversation_id"],
        "parent_message_id": row["parent_message_id"],
        "sequence_no": row["sequence_no"],
        "role": row["role"],
        "content": row["content"],
        "content_format": row["content_format"],
        "agent_id": row["agent_id"],
        "model": row["model"],
        "external_event_id": row["external_event_id"],
        "metadata": metadata,
        "created_by": row["created_by"],
        "created_at": ids.to_iso(row["created_at_ms"]),
        "completed_at": ids.to_iso(row["completed_at_ms"]),
        "deleted_at": ids.to_iso(row["deleted_at_ms"]),
    }


def next_sequence_no(conn: sqlite3.Connection, conversation_id: str) -> int:
    """Deleted messages keep their slot, so numbering never repeats."""
    row = conn.execute(
        "SELECT COALESCE(MAX(sequence_no), 0) + 1 FROM messages WHERE conversation_id = ?",
        (conversation_id,),
    ).fetchone()
    return row[0]


def get(
    conn: sqlite3.Connection, message_id: str, *, include_deleted: bool = False
) -> sqlite3.Row | None:
    clause = "" if include_deleted else " AND deleted_at_ms IS NULL"
    return conn.execute(
        f"SELECT {COLUMNS} FROM messages WHERE id = ?{clause}", (message_id,)
    ).fetchone()


def by_external_event(
    conn: sqlite3.Connection, conversation_id: str, external_event_id: str
) -> sqlite3.Row | None:
    return conn.execute(
        f"SELECT {COLUMNS} FROM messages WHERE conversation_id = ? AND external_event_id = ?",
        (conversation_id, external_event_id),
    ).fetchone()


def list_page(
    conn: sqlite3.Connection,
    conversation_id: str,
    *,
    after_sequence_no: int | None = None,
    before_sequence_no: int | None = None,
    limit: int = 200,
) -> list[sqlite3.Row]:
    """One page of live messages in transcript order.

    Paging is mandatory: a conversation with thousands of streamed events must
    never be loaded in one response, or the read transaction stays open long
    enough to stall WAL checkpointing.
    """
    params: list[Any] = [conversation_id]
    clauses = ["conversation_id = ?", "deleted_at_ms IS NULL"]
    if after_sequence_no is not None:
        clauses.append("sequence_no > ?")
        params.append(after_sequence_no)
    if before_sequence_no is not None:
        clauses.append("sequence_no < ?")
        params.append(before_sequence_no)
    params.append(max(1, min(limit, 1000)))
    return conn.execute(
        f"SELECT {COLUMNS} FROM messages WHERE {' AND '.join(clauses)}"
        " ORDER BY sequence_no LIMIT ?",
        params,
    ).fetchall()


def live_ids(conn: sqlite3.Connection, conversation_id: str) -> list[str]:
    return [
        row["id"]
        for row in conn.execute(
            "SELECT id FROM messages WHERE conversation_id = ? AND deleted_at_ms IS NULL",
            (conversation_id,),
        )
    ]


def insert(
    conn: sqlite3.Connection,
    *,
    conversation_id: str,
    role: str,
    content: str,
    now_ms: int,
    sequence_no: int | None = None,
    content_format: str = "plain",
    agent_id: str | None = None,
    model: str | None = None,
    external_event_id: str | None = None,
    metadata: dict[str, Any] | None = None,
    parent_message_id: str | None = None,
    created_by: str | None = None,
    completed_at_ms: int | None = None,
    message_id: str | None = None,
) -> tuple[str, int]:
    message_id = message_id or ids.new_id()
    if sequence_no is None:
        sequence_no = next_sequence_no(conn, conversation_id)
    conn.execute(
        "INSERT INTO messages (id, conversation_id, parent_message_id, sequence_no,"
        " role, content, content_format, agent_id, model, external_event_id,"
        " metadata_json, created_by, created_at_ms, completed_at_ms)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            message_id,
            conversation_id,
            parent_message_id,
            sequence_no,
            role,
            content,
            content_format,
            agent_id,
            model,
            external_event_id,
            json.dumps(metadata or {}, ensure_ascii=False),
            created_by,
            now_ms,
            completed_at_ms,
        ),
    )
    return message_id, sequence_no


def append_content(
    conn: sqlite3.Connection, message_id: str, delta: str, *, now_ms: int
) -> int:
    """Grow a streaming message in place, without reading it back first."""
    return conn.execute(
        "UPDATE messages SET content = content || ? WHERE id = ? AND deleted_at_ms IS NULL",
        (delta, message_id),
    ).rowcount


def complete(
    conn: sqlite3.Connection,
    message_id: str,
    *,
    now_ms: int,
    content: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> int:
    fields: dict[str, Any] = {"completed_at_ms": now_ms}
    if content is not None:
        fields["content"] = content
    if metadata is not None:
        fields["metadata_json"] = json.dumps(metadata, ensure_ascii=False)
    assignments = ", ".join(f"{key} = ?" for key in fields)
    return conn.execute(
        f"UPDATE messages SET {assignments} WHERE id = ? AND deleted_at_ms IS NULL",
        [*fields.values(), message_id],
    ).rowcount


def soft_delete(conn: sqlite3.Connection, message_id: str, now_ms: int) -> int:
    return conn.execute(
        "UPDATE messages SET deleted_at_ms = ? WHERE id = ? AND deleted_at_ms IS NULL",
        (now_ms, message_id),
    ).rowcount


# ------------------------------------------------------------- tool calls


def row_to_tool_call(row: sqlite3.Row) -> dict[str, Any]:
    try:
        payload = json.loads(row["input_json"])
    except (TypeError, ValueError):
        payload = {}
    return {
        "id": row["id"],
        "message_id": row["message_id"],
        "call_index": row["call_index"],
        "tool_name": row["tool_name"],
        "status": row["status"],
        "input": payload,
        "output_text": row["output_text"],
        "error_text": row["error_text"],
        "started_at": ids.to_iso(row["started_at_ms"]),
        "completed_at": ids.to_iso(row["completed_at_ms"]),
    }


def add_tool_call(
    conn: sqlite3.Connection,
    *,
    message_id: str,
    tool_name: str,
    status: str,
    call_index: int | None = None,
    payload: dict[str, Any] | None = None,
    output_text: str = "",
    error_text: str | None = None,
    started_at_ms: int | None = None,
    completed_at_ms: int | None = None,
) -> str:
    if call_index is None:
        call_index = conn.execute(
            "SELECT COALESCE(MAX(call_index), -1) + 1 FROM tool_calls WHERE message_id = ?",
            (message_id,),
        ).fetchone()[0]
    tool_call_id = ids.new_id()
    conn.execute(
        "INSERT INTO tool_calls (id, message_id, call_index, tool_name, status,"
        " input_json, output_text, error_text, started_at_ms, completed_at_ms)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            tool_call_id,
            message_id,
            call_index,
            tool_name,
            status,
            json.dumps(payload or {}, ensure_ascii=False),
            output_text,
            error_text,
            started_at_ms,
            completed_at_ms,
        ),
    )
    return tool_call_id


def update_tool_call(
    conn: sqlite3.Connection, tool_call_id: str, fields: dict[str, Any]
) -> int:
    if not fields:
        return 0
    assignments = ", ".join(f"{key} = ?" for key in fields)
    return conn.execute(
        f"UPDATE tool_calls SET {assignments} WHERE id = ?",
        [*fields.values(), tool_call_id],
    ).rowcount


def tool_calls_for(conn: sqlite3.Connection, message_ids: list[str]) -> list[sqlite3.Row]:
    if not message_ids:
        return []
    marks = ",".join("?" * len(message_ids))
    return conn.execute(
        f"SELECT * FROM tool_calls WHERE message_id IN ({marks})"
        " ORDER BY message_id, call_index",
        message_ids,
    ).fetchall()
