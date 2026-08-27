"""SQL for users, login sessions and idempotency records.

Repositories hold statements and row mapping only. Permission checks, hashing
and transaction boundaries belong to the service layer above them.
"""

from __future__ import annotations

import sqlite3
from typing import Any

from .. import ids

ROLES = ("admin", "member", "viewer")

USER_COLUMNS = (
    "id, username, email, email_verified_at_ms, display_name, role, is_active,"
    " created_at_ms, updated_at_ms, deleted_at_ms"
)


def row_to_user(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    return {
        "id": row["id"],
        "username": row["username"],
        "email": row["email"],
        "email_verified": row["email_verified_at_ms"] is not None,
        "display_name": row["display_name"],
        "role": row["role"],
        "is_active": bool(row["is_active"]),
        "created_at": ids.to_iso(row["created_at_ms"]),
        "updated_at": ids.to_iso(row["updated_at_ms"]),
        "deleted_at": ids.to_iso(row["deleted_at_ms"]),
    }


# ------------------------------------------------------------------- users


def create(
    conn: sqlite3.Connection,
    *,
    username: str,
    password_hash: str,
    display_name: str,
    role: str,
    email: str | None = None,
    now_ms: int,
) -> str:
    user_id = ids.new_id()
    conn.execute(
        "INSERT INTO users (id, username, password_hash, email, display_name, role,"
        " is_active, created_at_ms, updated_at_ms)"
        " VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?)",
        (user_id, username, password_hash, email, display_name, role, now_ms, now_ms),
    )
    return user_id


def get(conn: sqlite3.Connection, user_id: str) -> dict[str, Any] | None:
    return row_to_user(
        conn.execute(f"SELECT {USER_COLUMNS} FROM users WHERE id = ?", (user_id,)).fetchone()
    )


def get_by_username(conn: sqlite3.Connection, username: str) -> sqlite3.Row | None:
    """The raw row, password hash included — only `auth` should call this."""
    return conn.execute(
        f"SELECT {USER_COLUMNS}, password_hash FROM users WHERE username = ?",
        (username,),
    ).fetchone()


def get_by_email(conn: sqlite3.Connection, email: str) -> sqlite3.Row | None:
    """The raw row, password hash included — only `auth` should call this."""
    return conn.execute(
        f"SELECT {USER_COLUMNS}, password_hash FROM users WHERE email = ?", (email,)
    ).fetchone()


def list_all(conn: sqlite3.Connection, include_deleted: bool = False) -> list[dict[str, Any]]:
    where = "" if include_deleted else " WHERE deleted_at_ms IS NULL"
    rows = conn.execute(f"SELECT {USER_COLUMNS} FROM users{where} ORDER BY username")
    return [row_to_user(row) for row in rows]


def count(conn: sqlite3.Connection) -> int:
    return conn.execute("SELECT count(*) FROM users").fetchone()[0]


def update_fields(
    conn: sqlite3.Connection, user_id: str, fields: dict[str, Any], now_ms: int
) -> int:
    if not fields:
        return 0
    assignments = ", ".join(f"{key} = ?" for key in fields)
    params = list(fields.values()) + [now_ms, user_id]
    cursor = conn.execute(
        f"UPDATE users SET {assignments}, updated_at_ms = ? WHERE id = ?", params
    )
    return cursor.rowcount


# ---------------------------------------------------------------- sessions


def create_session(
    conn: sqlite3.Connection,
    *,
    user_id: str,
    token_hash: str,
    now_ms: int,
    expires_at_ms: int,
) -> str:
    session_id = ids.new_id()
    conn.execute(
        "INSERT INTO auth_sessions (id, user_id, token_hash, expires_at_ms,"
        " last_seen_at_ms, created_at_ms) VALUES (?, ?, ?, ?, ?, ?)",
        (session_id, user_id, token_hash, expires_at_ms, now_ms, now_ms),
    )
    return session_id


def session_by_token(
    conn: sqlite3.Connection, token_hash: str, now_ms: int
) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT s.id AS session_id, s.expires_at_ms, s.last_seen_at_ms,"
        f" {', '.join('u.' + c.strip() for c in USER_COLUMNS.split(','))}"
        " FROM auth_sessions s JOIN users u ON u.id = s.user_id"
        " WHERE s.token_hash = ? AND s.revoked_at_ms IS NULL"
        " AND s.expires_at_ms > ?",
        (token_hash, now_ms),
    ).fetchone()


def touch_session(conn: sqlite3.Connection, session_id: str, now_ms: int) -> None:
    conn.execute(
        "UPDATE auth_sessions SET last_seen_at_ms = ? WHERE id = ?", (now_ms, session_id)
    )


def revoke_session(conn: sqlite3.Connection, session_id: str, now_ms: int) -> int:
    return conn.execute(
        "UPDATE auth_sessions SET revoked_at_ms = ?"
        " WHERE id = ? AND revoked_at_ms IS NULL",
        (now_ms, session_id),
    ).rowcount


def revoke_sessions_for_user(conn: sqlite3.Connection, user_id: str, now_ms: int) -> int:
    """Used when an account is disabled or its password is reset."""
    return conn.execute(
        "UPDATE auth_sessions SET revoked_at_ms = ?"
        " WHERE user_id = ? AND revoked_at_ms IS NULL",
        (now_ms, user_id),
    ).rowcount


def purge_expired_sessions(conn: sqlite3.Connection, before_ms: int) -> int:
    return conn.execute(
        "DELETE FROM auth_sessions WHERE expires_at_ms < ?", (before_ms,)
    ).rowcount


# ---------------------------------------------------------- recovery codes


def revoke_codes(
    conn: sqlite3.Connection, *, user_id: str, purpose: str, now_ms: int
) -> int:
    return conn.execute(
        "UPDATE password_reset_codes SET consumed_at_ms = ?"
        " WHERE user_id = ? AND purpose = ? AND consumed_at_ms IS NULL",
        (now_ms, user_id, purpose),
    ).rowcount


def create_recovery_code(
    conn: sqlite3.Connection,
    *,
    user_id: str,
    purpose: str,
    email: str,
    code_salt: str,
    code_hash: str,
    expires_at_ms: int,
    now_ms: int,
) -> str:
    code_id = ids.new_id()
    conn.execute(
        "INSERT INTO password_reset_codes (id, user_id, purpose, email, code_salt,"
        " code_hash, expires_at_ms, created_at_ms) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (code_id, user_id, purpose, email, code_salt, code_hash, expires_at_ms, now_ms),
    )
    return code_id


def active_recovery_code(
    conn: sqlite3.Connection, *, email: str, purpose: str, now_ms: int
) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM password_reset_codes WHERE email = ? AND purpose = ?"
        " AND consumed_at_ms IS NULL AND expires_at_ms > ?"
        " ORDER BY created_at_ms DESC LIMIT 1",
        (email, purpose, now_ms),
    ).fetchone()


def consume_recovery_code(conn: sqlite3.Connection, code_id: str, now_ms: int) -> int:
    return conn.execute(
        "UPDATE password_reset_codes SET consumed_at_ms = ?"
        " WHERE id = ? AND consumed_at_ms IS NULL",
        (now_ms, code_id),
    ).rowcount


def increment_recovery_attempts(conn: sqlite3.Connection, code_id: str) -> None:
    conn.execute(
        "UPDATE password_reset_codes SET attempts = attempts + 1 WHERE id = ?", (code_id,)
    )
