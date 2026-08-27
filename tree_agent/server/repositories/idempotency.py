"""Replay records for `Idempotency-Key`, so a network retry cannot duplicate work."""

from __future__ import annotations

import sqlite3

# A day is long enough to cover any client retry loop and short enough that the
# table stays small next to 100 GiB of attachments.
DEFAULT_TTL_MS = 24 * 60 * 60 * 1000


def lookup(
    conn: sqlite3.Connection, user_id: str, request_key: str, now_ms: int
) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT request_fingerprint, status_code, response_json FROM idempotency_keys"
        " WHERE user_id = ? AND request_key = ? AND expires_at_ms > ?",
        (user_id, request_key, now_ms),
    ).fetchone()


def remember(
    conn: sqlite3.Connection,
    *,
    user_id: str,
    request_key: str,
    fingerprint: str,
    status_code: int,
    response_json: str,
    now_ms: int,
    ttl_ms: int = DEFAULT_TTL_MS,
) -> None:
    conn.execute(
        "INSERT INTO idempotency_keys (user_id, request_key, request_fingerprint,"
        " status_code, response_json, created_at_ms, expires_at_ms)"
        " VALUES (?, ?, ?, ?, ?, ?, ?)"
        " ON CONFLICT(user_id, request_key) DO UPDATE SET"
        "   request_fingerprint = excluded.request_fingerprint,"
        "   status_code = excluded.status_code,"
        "   response_json = excluded.response_json,"
        "   created_at_ms = excluded.created_at_ms,"
        "   expires_at_ms = excluded.expires_at_ms",
        (
            user_id,
            request_key,
            fingerprint,
            status_code,
            response_json,
            now_ms,
            now_ms + ttl_ms,
        ),
    )


def purge_expired(conn: sqlite3.Connection, now_ms: int) -> int:
    return conn.execute(
        "DELETE FROM idempotency_keys WHERE expires_at_ms <= ?", (now_ms,)
    ).rowcount
