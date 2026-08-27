"""Who may see and change which project.

The rule, stated once: for a given user, walk from the project towards the root
and take the first membership *that user* has. Other people's grants on the way
up are irrelevant. A grant on a sub-project overrides the one it inherits, in
either direction — a viewer on the parent can be an editor on one child, and an
editor on the parent can be narrowed to viewer on another.

Two deliberate choices about what leaks:

* a project the caller cannot read reports 404, not 403, so nobody can map the
  tree by trying ids;
* a project they can read but not write reports 403, because at that point they
  already know it exists.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Any

from ..errors import NotFound, PermissionDenied
from ..repositories import projects as projects_repo

OWNER, EDITOR, VIEWER = "owner", "editor", "viewer"
_RANK = {VIEWER: 0, EDITOR: 1, OWNER: 2}

MAX_DEPTH = projects_repo.MAX_DEPTH


@dataclass(frozen=True)
class Actor:
    """The authenticated caller, as far as authorisation is concerned."""

    id: str
    username: str
    display_name: str
    role: str
    is_active: bool = True
    session_id: str | None = None

    @property
    def is_admin(self) -> bool:
        return self.role == "admin"

    @property
    def is_read_only(self) -> bool:
        """A `viewer` account cannot write anywhere, whatever its grants say."""
        return self.role == "viewer"


def _cap(actor: Actor, permission: str | None) -> str | None:
    if permission is None:
        return None
    if actor.is_read_only and _RANK[permission] > _RANK[VIEWER]:
        return VIEWER
    return permission


def permission_for(
    conn: sqlite3.Connection, actor: Actor, project_id: str, *, include_deleted: bool = False
) -> str | None:
    """Effective permission on one project, or None."""
    if actor.is_admin:
        row = projects_repo.get(conn, project_id, include_deleted=include_deleted)
        return OWNER if row is not None else None

    seen: set[str] = set()
    current: str | None = project_id
    first = True
    depth = 0
    while current and current not in seen and depth < MAX_DEPTH:
        seen.add(current)
        depth += 1
        row = projects_repo.get(conn, current, include_deleted=True)
        if row is None:
            return None
        if first:
            first = False
            if row["deleted_at_ms"] is not None and not include_deleted:
                return None
        granted = projects_repo.membership_for(conn, current, actor.id)
        if granted:
            return _cap(actor, granted)
        current = row["parent_id"]
    return None


def visible_projects(conn: sqlite3.Connection, actor: Actor) -> dict[str, str]:
    """Every live project the caller can read, mapped to its permission.

    One downward pass rather than one upward walk per project — search filters
    and tree listings both need the whole set at once.
    """
    rows = projects_repo.list_live(conn)
    if actor.is_admin:
        return {row["id"]: OWNER for row in rows}

    grants = projects_repo.memberships_for_user(conn, actor.id)
    by_parent: dict[str | None, list[sqlite3.Row]] = {}
    for row in rows:
        by_parent.setdefault(row["parent_id"], []).append(row)

    visible: dict[str, str] = {}
    stack: list[tuple[str | None, str | None, int]] = [(None, None, 0)]
    while stack:
        parent_id, inherited, depth = stack.pop()
        if depth > MAX_DEPTH:
            continue
        for row in by_parent.get(parent_id, ()):
            permission = grants.get(row["id"], inherited)
            if permission is not None:
                visible[row["id"]] = _cap(actor, permission)
            stack.append((row["id"], permission, depth + 1))
    return visible


# ------------------------------------------------------------- assertions


def require_read(conn: sqlite3.Connection, actor: Actor, project_id: str) -> str:
    permission = permission_for(conn, actor, project_id)
    if permission is None:
        raise NotFound("找不到專案")
    return permission


def require_write(conn: sqlite3.Connection, actor: Actor, project_id: str) -> str:
    permission = require_read(conn, actor, project_id)
    if permission not in (OWNER, EDITOR):
        raise PermissionDenied("沒有這個專案的編輯權限")
    return permission


def require_owner(conn: sqlite3.Connection, actor: Actor, project_id: str) -> str:
    permission = require_read(conn, actor, project_id)
    if permission != OWNER:
        raise PermissionDenied("只有專案 owner 或系統管理員可以調整權限")
    return permission


def require_admin(actor: Actor) -> None:
    if not actor.is_admin:
        raise PermissionDenied("需要系統管理員權限")


def require_root_write(actor: Actor) -> None:
    """Top-level projects have no parent to inherit a grant from."""
    if not actor.is_admin:
        raise PermissionDenied("只有系統管理員可以建立最上層專案")


def can_write(permission: str | None) -> bool:
    return permission in (OWNER, EDITOR)


def describe(conn: sqlite3.Connection, actor: Actor, project_id: str) -> dict[str, Any]:
    permission = permission_for(conn, actor, project_id)
    return {
        "permission": permission,
        "can_read": permission is not None,
        "can_write": can_write(permission),
        "can_manage": permission == OWNER,
    }
