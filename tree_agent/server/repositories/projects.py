"""SQL for the project tree and its access grants.

Soft deletes stamp every row in the subtree with the *same* `deleted_at_ms`.
Restore then matches on that exact value, so a child that was already deleted
last week stays deleted when its parent is restored today.
"""

from __future__ import annotations

import sqlite3
from typing import Any, Iterable

from .. import ids

# Guards every recursive walk. The service refuses to create cycles, but a
# corrupted row must not turn a query into an infinite loop.
MAX_DEPTH = 200

COLUMNS = (
    "id, parent_id, name, sort_key, cwd, model, sandbox, claude_permission,"
    " prompt, is_expanded, revision, created_by, created_at_ms, updated_at_ms,"
    " deleted_at_ms, deleted_by"
)

# Settings a project may override for itself and its descendants.
SETTING_KEYS = ("cwd", "model", "sandbox", "claude_permission", "prompt")

# Only these columns may be named by a caller-supplied `fields` mapping. The
# service layer already allowlists what a request may change; this is the
# backstop that keeps a future caller from turning a key into SQL.
UPDATABLE = frozenset(
    {"parent_id", "name", "sort_key", "cwd", "model", "sandbox",
     "claude_permission", "prompt", "is_expanded"}
)


def _assignments(fields: dict[str, Any]) -> str:
    unknown = set(fields) - UPDATABLE
    if unknown:
        raise ValueError(f"not updatable columns: {sorted(unknown)}")
    return ", ".join(f"{key} = ?" for key in fields)



def row_to_project(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    return {
        "id": row["id"],
        "parent_id": row["parent_id"],
        "name": row["name"],
        "sort_key": row["sort_key"],
        "cwd": row["cwd"],
        "model": row["model"],
        "sandbox": row["sandbox"],
        "claude_permission": row["claude_permission"],
        "prompt": row["prompt"],
        "is_expanded": bool(row["is_expanded"]),
        "revision": row["revision"],
        "created_by": row["created_by"],
        "created_at": ids.to_iso(row["created_at_ms"]),
        "updated_at": ids.to_iso(row["updated_at_ms"]),
        "deleted_at": ids.to_iso(row["deleted_at_ms"]),
    }


# ---------------------------------------------------------------- read side


def get(
    conn: sqlite3.Connection, project_id: str, *, include_deleted: bool = False
) -> sqlite3.Row | None:
    clause = "" if include_deleted else " AND deleted_at_ms IS NULL"
    return conn.execute(
        f"SELECT {COLUMNS} FROM projects WHERE id = ?{clause}", (project_id,)
    ).fetchone()


def list_live(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        f"SELECT {COLUMNS} FROM projects WHERE deleted_at_ms IS NULL"
        " ORDER BY sort_key, name"
    ).fetchall()


def children(conn: sqlite3.Connection, parent_id: str | None) -> list[sqlite3.Row]:
    clause = "parent_id IS NULL" if parent_id is None else "parent_id = ?"
    params = () if parent_id is None else (parent_id,)
    return conn.execute(
        f"SELECT {COLUMNS} FROM projects WHERE {clause} AND deleted_at_ms IS NULL"
        " ORDER BY sort_key",
        params,
    ).fetchall()


def sibling_sort_keys(conn: sqlite3.Connection, parent_id: str | None) -> list[str]:
    return [row["sort_key"] for row in children(conn, parent_id)]


def ancestors(conn: sqlite3.Connection, project_id: str) -> list[sqlite3.Row]:
    """Nearest-first chain of ancestors, including `project_id` itself first.

    Deleted ancestors are included: a restore has to be able to see that its old
    parent is gone.
    """
    chain: list[sqlite3.Row] = []
    seen: set[str] = set()
    current: str | None = project_id
    while current and current not in seen and len(chain) < MAX_DEPTH:
        seen.add(current)
        row = get(conn, current, include_deleted=True)
        if row is None:
            break
        chain.append(row)
        current = row["parent_id"]
    return chain


def descendant_ids(conn: sqlite3.Connection, project_id: str, *, live_only: bool = True) -> list[str]:
    """Every project under `project_id`, itself included."""
    clause = " AND p.deleted_at_ms IS NULL" if live_only else ""
    rows = conn.execute(
        "WITH RECURSIVE sub(id, depth) AS ("
        "  SELECT ?, 0"
        "  UNION ALL"
        f"  SELECT p.id, sub.depth + 1 FROM projects p JOIN sub ON p.parent_id = sub.id"
        f"  WHERE sub.depth < ?{clause}"
        ") SELECT id FROM sub",
        (project_id, MAX_DEPTH),
    ).fetchall()
    return [row["id"] for row in rows]


def live_name_taken(
    conn: sqlite3.Connection, parent_id: str | None, name: str, *, exclude_id: str | None = None
) -> bool:
    clause = "parent_id IS NULL" if parent_id is None else "parent_id = ?"
    params: list[Any] = [] if parent_id is None else [parent_id]
    params.append(name)
    exclusion = ""
    if exclude_id:
        exclusion = " AND id <> ?"
        params.append(exclude_id)
    return (
        conn.execute(
            f"SELECT 1 FROM projects WHERE {clause} AND name = ? COLLATE NOCASE"
            f" AND deleted_at_ms IS NULL{exclusion} LIMIT 1",
            params,
        ).fetchone()
        is not None
    )


# --------------------------------------------------------------- write side


def insert(
    conn: sqlite3.Connection,
    *,
    parent_id: str | None,
    name: str,
    sort_key: str,
    created_by: str,
    now_ms: int,
    settings: dict[str, Any] | None = None,
) -> str:
    project_id = ids.new_id()
    values = {key: (settings or {}).get(key) for key in SETTING_KEYS}
    conn.execute(
        "INSERT INTO projects (id, parent_id, name, sort_key, cwd, model, sandbox,"
        " claude_permission, prompt, is_expanded, revision, created_by,"
        " created_at_ms, updated_at_ms) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1, 1, ?, ?, ?)",
        (
            project_id,
            parent_id,
            name,
            sort_key,
            values["cwd"],
            values["model"],
            values["sandbox"],
            values["claude_permission"],
            values["prompt"],
            created_by,
            now_ms,
            now_ms,
        ),
    )
    return project_id


def update(
    conn: sqlite3.Connection,
    project_id: str,
    fields: dict[str, Any],
    *,
    expected_revision: int | None,
    now_ms: int,
) -> int:
    """Bump the revision and apply `fields`. Returns the number of rows changed."""
    assignments = _assignments(fields)
    if assignments:
        assignments += ", "
    params = list(fields.values()) + [now_ms, project_id]
    revision_clause = ""
    if expected_revision is not None:
        revision_clause = " AND revision = ?"
        params.append(expected_revision)
    return conn.execute(
        f"UPDATE projects SET {assignments}revision = revision + 1,"
        f" updated_at_ms = ? WHERE id = ? AND deleted_at_ms IS NULL{revision_clause}",
        params,
    ).rowcount


def set_expanded(conn: sqlite3.Connection, project_id: str, expanded: bool) -> int:
    """Tree expansion is presentation state; it deliberately skips the revision."""
    return conn.execute(
        "UPDATE projects SET is_expanded = ? WHERE id = ? AND deleted_at_ms IS NULL",
        (1 if expanded else 0, project_id),
    ).rowcount


def soft_delete_subtree(
    conn: sqlite3.Connection, project_id: str, *, deleted_by: str, now_ms: int
) -> dict[str, list[str]]:
    """Mark the project, its live descendants, their conversations and messages.

    Returns the ids touched, so the caller can drop exactly those rows from the
    search indexes in the same transaction.
    """
    project_ids = descendant_ids(conn, project_id, live_only=True)
    if not project_ids:
        return {"projects": [], "conversations": [], "messages": []}

    placeholders = ",".join("?" * len(project_ids))
    conversation_ids = [
        row["id"]
        for row in conn.execute(
            f"SELECT id FROM conversations WHERE project_id IN ({placeholders})"
            " AND deleted_at_ms IS NULL",
            project_ids,
        )
    ]
    message_ids = _live_message_ids(conn, conversation_ids)

    conn.execute(
        f"UPDATE projects SET deleted_at_ms = ?, deleted_by = ?,"
        f" revision = revision + 1 WHERE id IN ({placeholders})",
        [now_ms, deleted_by, *project_ids],
    )
    _mark_conversations(conn, conversation_ids, deleted_by, now_ms)
    _mark_messages(conn, message_ids, now_ms)
    return {
        "projects": project_ids,
        "conversations": conversation_ids,
        "messages": message_ids,
    }


def restore_subtree(
    conn: sqlite3.Connection, project_id: str, deleted_at_ms: int
) -> dict[str, list[str]]:
    """Undo one `soft_delete_subtree`, identified by its exact timestamp."""
    project_ids = [
        row["id"]
        for row in conn.execute(
            "WITH RECURSIVE sub(id, depth) AS ("
            "  SELECT ?, 0"
            "  UNION ALL"
            "  SELECT p.id, sub.depth + 1 FROM projects p JOIN sub ON p.parent_id = sub.id"
            "  WHERE sub.depth < ? AND p.deleted_at_ms = ?"
            ") SELECT id FROM sub",
            (project_id, MAX_DEPTH, deleted_at_ms),
        )
    ]
    if not project_ids:
        return {"projects": [], "conversations": [], "messages": []}

    placeholders = ",".join("?" * len(project_ids))
    conversation_ids = [
        row["id"]
        for row in conn.execute(
            f"SELECT id FROM conversations WHERE project_id IN ({placeholders})"
            " AND deleted_at_ms = ?",
            [*project_ids, deleted_at_ms],
        )
    ]
    message_ids: list[str] = []
    if conversation_ids:
        marks = ",".join("?" * len(conversation_ids))
        message_ids = [
            row["id"]
            for row in conn.execute(
                f"SELECT id FROM messages WHERE conversation_id IN ({marks})"
                " AND deleted_at_ms = ?",
                [*conversation_ids, deleted_at_ms],
            )
        ]

    conn.execute(
        f"UPDATE projects SET deleted_at_ms = NULL, deleted_by = NULL,"
        f" revision = revision + 1 WHERE id IN ({placeholders})",
        project_ids,
    )
    if conversation_ids:
        marks = ",".join("?" * len(conversation_ids))
        conn.execute(
            f"UPDATE conversations SET deleted_at_ms = NULL, deleted_by = NULL,"
            f" revision = revision + 1 WHERE id IN ({marks})",
            conversation_ids,
        )
    if message_ids:
        marks = ",".join("?" * len(message_ids))
        conn.execute(
            f"UPDATE messages SET deleted_at_ms = NULL WHERE id IN ({marks})",
            message_ids,
        )
    return {
        "projects": project_ids,
        "conversations": conversation_ids,
        "messages": message_ids,
    }


def _live_message_ids(conn: sqlite3.Connection, conversation_ids: list[str]) -> list[str]:
    if not conversation_ids:
        return []
    marks = ",".join("?" * len(conversation_ids))
    return [
        row["id"]
        for row in conn.execute(
            f"SELECT id FROM messages WHERE conversation_id IN ({marks})"
            " AND deleted_at_ms IS NULL",
            conversation_ids,
        )
    ]


def _mark_conversations(
    conn: sqlite3.Connection, conversation_ids: list[str], deleted_by: str, now_ms: int
) -> None:
    if not conversation_ids:
        return
    marks = ",".join("?" * len(conversation_ids))
    conn.execute(
        f"UPDATE conversations SET deleted_at_ms = ?, deleted_by = ?,"
        f" revision = revision + 1 WHERE id IN ({marks})",
        [now_ms, deleted_by, *conversation_ids],
    )


def _mark_messages(conn: sqlite3.Connection, message_ids: list[str], now_ms: int) -> None:
    if not message_ids:
        return
    marks = ",".join("?" * len(message_ids))
    conn.execute(
        f"UPDATE messages SET deleted_at_ms = ? WHERE id IN ({marks})",
        [now_ms, *message_ids],
    )


# ------------------------------------------------------------- memberships

PERMISSIONS = ("owner", "editor", "viewer")
WRITE_PERMISSIONS = ("owner", "editor")


def memberships_for_user(conn: sqlite3.Connection, user_id: str) -> dict[str, str]:
    return {
        row["project_id"]: row["permission"]
        for row in conn.execute(
            "SELECT project_id, permission FROM project_memberships WHERE user_id = ?",
            (user_id,),
        )
    }


def memberships_for_project(conn: sqlite3.Connection, project_id: str) -> list[dict[str, Any]]:
    return [
        {
            "project_id": row["project_id"],
            "user_id": row["user_id"],
            "username": row["username"],
            "display_name": row["display_name"],
            "permission": row["permission"],
            "granted_by": row["granted_by"],
            "created_at": ids.to_iso(row["created_at_ms"]),
        }
        for row in conn.execute(
            "SELECT m.*, u.username, u.display_name FROM project_memberships m"
            " JOIN users u ON u.id = m.user_id WHERE m.project_id = ?"
            " ORDER BY u.username",
            (project_id,),
        )
    ]


def membership_for(
    conn: sqlite3.Connection, project_id: str, user_id: str
) -> str | None:
    row = conn.execute(
        "SELECT permission FROM project_memberships"
        " WHERE project_id = ? AND user_id = ?",
        (project_id, user_id),
    ).fetchone()
    return row["permission"] if row else None


def grant(
    conn: sqlite3.Connection,
    *,
    project_id: str,
    user_id: str,
    permission: str,
    granted_by: str,
    now_ms: int,
) -> None:
    conn.execute(
        "INSERT INTO project_memberships (project_id, user_id, permission,"
        " granted_by, created_at_ms) VALUES (?, ?, ?, ?, ?)"
        " ON CONFLICT(project_id, user_id) DO UPDATE SET"
        "   permission = excluded.permission, granted_by = excluded.granted_by",
        (project_id, user_id, permission, granted_by, now_ms),
    )


def revoke(conn: sqlite3.Connection, project_id: str, user_id: str) -> int:
    return conn.execute(
        "DELETE FROM project_memberships WHERE project_id = ? AND user_id = ?",
        (project_id, user_id),
    ).rowcount


def revoke_all(conn: sqlite3.Connection, project_ids: Iterable[str]) -> int:
    project_ids = list(project_ids)
    if not project_ids:
        return 0
    marks = ",".join("?" * len(project_ids))
    return conn.execute(
        f"DELETE FROM project_memberships WHERE project_id IN ({marks})", project_ids
    ).rowcount
