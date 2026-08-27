"""SQL for attachment bytes, their staging area, and message links.

Bytes live in SQLite, but never as one 20 MiB parameter: everything moves a
chunk at a time so peak memory stays at one chunk per request, whichever
direction it is going.
"""

from __future__ import annotations

import sqlite3
from typing import Any, Iterator

from .. import ids

CHUNK_SIZE = 1024 * 1024  # 1 MiB
MAX_BYTES = 20 * 1024 * 1024  # matches the CHECK constraint on both tables
UPLOAD_TTL_MS = 24 * 60 * 60 * 1000

UPLOAD_STATUSES = ("uploading", "committed", "expired", "failed")


def row_to_attachment(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    return {
        "id": row["id"],
        "sha256": row["sha256"],
        "file_name": row["file_name"],
        "mime_type": row["mime_type"],
        "byte_size": row["byte_size"],
        "chunk_count": row["chunk_count"],
        "created_by": row["created_by"],
        "created_at": ids.to_iso(row["created_at_ms"]),
        "deleted_at": ids.to_iso(row["deleted_at_ms"]),
    }


# ------------------------------------------------------------------ uploads


def create_upload(
    conn: sqlite3.Connection,
    *,
    user_id: str,
    file_name: str,
    mime_type: str,
    expected_byte_size: int,
    expected_sha256: str | None,
    target_message_id: str | None,
    now_ms: int,
) -> str:
    upload_id = ids.new_id()
    conn.execute(
        "INSERT INTO attachment_uploads (id, user_id, target_message_id, file_name,"
        " mime_type, expected_byte_size, expected_sha256, status, received_byte_size,"
        " created_at_ms, expires_at_ms) VALUES (?, ?, ?, ?, ?, ?, ?, 'uploading', 0, ?, ?)",
        (
            upload_id,
            user_id,
            target_message_id,
            file_name,
            mime_type,
            expected_byte_size,
            expected_sha256,
            now_ms,
            now_ms + UPLOAD_TTL_MS,
        ),
    )
    return upload_id


def get_upload(conn: sqlite3.Connection, upload_id: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM attachment_uploads WHERE id = ?", (upload_id,)
    ).fetchone()


def put_upload_chunk(
    conn: sqlite3.Connection, upload_id: str, chunk_no: int, payload: bytes
) -> None:
    """Store one chunk. Re-sending the same number is a retry, not a duplicate."""
    previous = conn.execute(
        "SELECT length(bytes) AS size FROM attachment_upload_chunks"
        " WHERE upload_id = ? AND chunk_no = ?",
        (upload_id, chunk_no),
    ).fetchone()
    conn.execute(
        "INSERT INTO attachment_upload_chunks (upload_id, chunk_no, bytes)"
        " VALUES (?, ?, ?)"
        " ON CONFLICT(upload_id, chunk_no) DO UPDATE SET bytes = excluded.bytes",
        (upload_id, chunk_no, payload),
    )
    delta = len(payload) - (previous["size"] if previous else 0)
    conn.execute(
        "UPDATE attachment_uploads SET received_byte_size = received_byte_size + ?"
        " WHERE id = ?",
        (delta, upload_id),
    )


def upload_chunk_numbers(conn: sqlite3.Connection, upload_id: str) -> list[int]:
    return [
        row["chunk_no"]
        for row in conn.execute(
            "SELECT chunk_no FROM attachment_upload_chunks WHERE upload_id = ?"
            " ORDER BY chunk_no",
            (upload_id,),
        )
    ]


def iter_upload_chunks(conn: sqlite3.Connection, upload_id: str) -> Iterator[bytes]:
    """One chunk at a time — used to hash without materialising the whole file."""
    for row in conn.execute(
        "SELECT bytes FROM attachment_upload_chunks WHERE upload_id = ? ORDER BY chunk_no",
        (upload_id,),
    ):
        yield row["bytes"]


def set_upload_status(
    conn: sqlite3.Connection,
    upload_id: str,
    status: str,
    *,
    committed_attachment_id: str | None = None,
) -> int:
    return conn.execute(
        "UPDATE attachment_uploads SET status = ?, committed_attachment_id = ?"
        " WHERE id = ?",
        (status, committed_attachment_id, upload_id),
    ).rowcount


def drop_upload_chunks(conn: sqlite3.Connection, upload_id: str) -> int:
    return conn.execute(
        "DELETE FROM attachment_upload_chunks WHERE upload_id = ?", (upload_id,)
    ).rowcount


def expire_stale_uploads(conn: sqlite3.Connection, now_ms: int) -> int:
    stale = [
        row["id"]
        for row in conn.execute(
            "SELECT id FROM attachment_uploads WHERE status = 'uploading'"
            " AND expires_at_ms <= ?",
            (now_ms,),
        )
    ]
    for upload_id in stale:
        drop_upload_chunks(conn, upload_id)
        set_upload_status(conn, upload_id, "expired")
    return len(stale)


# -------------------------------------------------------------- attachments


def by_hash(conn: sqlite3.Connection, sha256: str, byte_size: int) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM attachments WHERE sha256 = ? AND byte_size = ?",
        (sha256, byte_size),
    ).fetchone()


def get(conn: sqlite3.Connection, attachment_id: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM attachments WHERE id = ?", (attachment_id,)
    ).fetchone()


def create_from_upload(
    conn: sqlite3.Connection,
    *,
    upload_id: str,
    sha256: str,
    file_name: str,
    mime_type: str,
    byte_size: int,
    chunk_count: int,
    created_by: str,
    now_ms: int,
) -> str:
    """Promote staged chunks to a real attachment without leaving SQLite."""
    attachment_id = ids.new_id()
    conn.execute(
        "INSERT INTO attachments (id, sha256, file_name, mime_type, byte_size,"
        " chunk_count, created_by, created_at_ms) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (attachment_id, sha256, file_name, mime_type, byte_size, chunk_count, created_by, now_ms),
    )
    conn.execute(
        "INSERT INTO attachment_chunks (attachment_id, chunk_no, bytes)"
        " SELECT ?, chunk_no, bytes FROM attachment_upload_chunks WHERE upload_id = ?",
        (attachment_id, upload_id),
    )
    return attachment_id


def create_with_chunks(
    conn: sqlite3.Connection,
    *,
    sha256: str,
    file_name: str,
    mime_type: str,
    byte_size: int,
    chunks: list[bytes],
    created_by: str,
    now_ms: int,
) -> str:
    """Direct path for the legacy importer, which reads from disk not an upload."""
    attachment_id = ids.new_id()
    conn.execute(
        "INSERT INTO attachments (id, sha256, file_name, mime_type, byte_size,"
        " chunk_count, created_by, created_at_ms) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (attachment_id, sha256, file_name, mime_type, byte_size, len(chunks), created_by, now_ms),
    )
    conn.executemany(
        "INSERT INTO attachment_chunks (attachment_id, chunk_no, bytes) VALUES (?, ?, ?)",
        [(attachment_id, index, payload) for index, payload in enumerate(chunks)],
    )
    return attachment_id


def iter_chunks(conn: sqlite3.Connection, attachment_id: str) -> Iterator[bytes]:
    for row in conn.execute(
        "SELECT bytes FROM attachment_chunks WHERE attachment_id = ? ORDER BY chunk_no",
        (attachment_id,),
    ):
        yield row["bytes"]


def delete_bytes(conn: sqlite3.Connection, attachment_id: str) -> int:
    """Destroy an attachment for good. Callers must prove it is unreferenced."""
    conn.execute(
        "DELETE FROM attachment_chunks WHERE attachment_id = ?", (attachment_id,)
    )
    # The upload record that produced it is history, not a reference; keep the
    # row and let go of the pointer.
    conn.execute(
        "UPDATE attachment_uploads SET committed_attachment_id = NULL"
        " WHERE committed_attachment_id = ?",
        (attachment_id,),
    )
    return conn.execute("DELETE FROM attachments WHERE id = ?", (attachment_id,)).rowcount


def purge_finished_uploads(conn: sqlite3.Connection, before_ms: int) -> int:
    """Drop upload records that have been in a terminal state for a while."""
    return conn.execute(
        "DELETE FROM attachment_uploads WHERE status <> 'uploading' AND created_at_ms < ?",
        (before_ms,),
    ).rowcount


# ----------------------------------------------------------------- linkage


def link(
    conn: sqlite3.Connection, message_id: str, attachment_id: str, display_order: int | None = None
) -> int:
    if display_order is None:
        display_order = conn.execute(
            "SELECT COALESCE(MAX(display_order), -1) + 1 FROM message_attachments"
            " WHERE message_id = ?",
            (message_id,),
        ).fetchone()[0]
    conn.execute(
        "INSERT INTO message_attachments (message_id, attachment_id, display_order)"
        " VALUES (?, ?, ?) ON CONFLICT(message_id, attachment_id) DO NOTHING",
        (message_id, attachment_id, display_order),
    )
    return display_order


def unlink(conn: sqlite3.Connection, message_id: str, attachment_id: str) -> int:
    return conn.execute(
        "DELETE FROM message_attachments WHERE message_id = ? AND attachment_id = ?",
        (message_id, attachment_id),
    ).rowcount


def for_messages(conn: sqlite3.Connection, message_ids: list[str]) -> list[sqlite3.Row]:
    if not message_ids:
        return []
    marks = ",".join("?" * len(message_ids))
    return conn.execute(
        "SELECT ma.message_id, ma.display_order, a.* FROM message_attachments ma"
        f" JOIN attachments a ON a.id = ma.attachment_id"
        f" WHERE ma.message_id IN ({marks}) ORDER BY ma.message_id, ma.display_order",
        message_ids,
    ).fetchall()


def messages_referencing(conn: sqlite3.Connection, attachment_id: str) -> list[str]:
    return [
        row["message_id"]
        for row in conn.execute(
            "SELECT message_id FROM message_attachments WHERE attachment_id = ?",
            (attachment_id,),
        )
    ]


def is_live_referenced(conn: sqlite3.Connection, attachment_id: str) -> bool:
    """A reference counts as live only when its message *and* conversation are."""
    return (
        conn.execute(
            "SELECT 1 FROM message_attachments ma"
            " JOIN messages m ON m.id = ma.message_id"
            " JOIN conversations c ON c.id = m.conversation_id"
            " WHERE ma.attachment_id = ? AND m.deleted_at_ms IS NULL"
            " AND c.deleted_at_ms IS NULL LIMIT 1",
            (attachment_id,),
        ).fetchone()
        is not None
    )


def readable_by_message(
    conn: sqlite3.Connection, attachment_id: str
) -> list[sqlite3.Row]:
    """Every (conversation, project) an attachment is reachable through.

    Download authorisation is by conversation, not by attachment id: the same
    bytes are deduplicated across messages, so holding the id proves nothing.
    """
    return conn.execute(
        "SELECT DISTINCT c.id AS conversation_id, c.project_id FROM message_attachments ma"
        " JOIN messages m ON m.id = ma.message_id"
        " JOIN conversations c ON c.id = m.conversation_id"
        " WHERE ma.attachment_id = ? AND m.deleted_at_ms IS NULL"
        " AND c.deleted_at_ms IS NULL",
        (attachment_id,),
    ).fetchall()
