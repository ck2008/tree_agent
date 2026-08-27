"""Full-text search across projects, conversations and messages.

The order matters and is not negotiable: work out what the caller may read
first, hand only those project ids to FTS, then confirm against the base tables
that every hit is still live. A search index is a cache — treating it as the
authority on either visibility or existence is how deleted content resurfaces.
"""

from __future__ import annotations

from typing import Any

from ..db import Database
from ..errors import ValidationError
from ..repositories import projects as projects_repo
from ..repositories import search as search_repo
from .access import Actor, visible_projects

KINDS = ("project", "conversation", "message")
MAX_LIMIT = 100


class SearchService:
    def __init__(self, db: Database) -> None:
        self.db = db

    def search(
        self,
        actor: Actor,
        query: str,
        *,
        kinds: tuple[str, ...] = KINDS,
        limit: int = 20,
        offset: int = 0,
    ) -> dict[str, Any]:
        unknown = set(kinds) - set(KINDS)
        if unknown:
            raise ValidationError(f"未知的搜尋類型：{', '.join(sorted(unknown))}")
        limit = max(1, min(int(limit), MAX_LIMIT))
        offset = max(0, int(offset))
        match = search_repo.build_match(query)
        if not match:
            return {"query": query, "results": [], "total_kinds": {}}

        with self.db.read() as conn:
            allowed = list(visible_projects(conn, actor))
            if not allowed:
                return {"query": query, "results": [], "total_kinds": {}}

            paths = _path_cache(conn)
            results: list[dict[str, Any]] = []
            counts: dict[str, int] = {}

            if "project" in kinds:
                rows = search_repo.search_projects(conn, match, allowed, limit, offset)
                counts["project"] = len(rows)
                for row in rows:
                    results.append(
                        {
                            "kind": "project",
                            "id": row["project_id"],
                            "project_id": row["project_id"],
                            "title": paths.get(row["project_id"], row["name"]),
                            "path": paths.get(row["project_id"], row["name"]),
                            "summary": search_repo.summarise(row["prompt"] or "", query),
                            "rank": row["rank"],
                        }
                    )

            if "conversation" in kinds:
                rows = search_repo.search_conversations(conn, match, allowed, limit, offset)
                counts["conversation"] = len(rows)
                for row in rows:
                    path = paths.get(row["project_id"], "")
                    results.append(
                        {
                            "kind": "conversation",
                            "id": row["conversation_id"],
                            "conversation_id": row["conversation_id"],
                            "project_id": row["project_id"],
                            "title": row["name"],
                            "path": f"{path} / {row['name']}" if path else row["name"],
                            "summary": "",
                            "rank": row["rank"],
                        }
                    )

            if "message" in kinds:
                rows = search_repo.search_messages(conn, match, allowed, limit, offset)
                counts["message"] = len(rows)
                for row in rows:
                    path = paths.get(row["project_id"], "")
                    title = row["conversation_name"]
                    results.append(
                        {
                            "kind": "message",
                            "id": row["message_id"],
                            "message_id": row["message_id"],
                            "conversation_id": row["conversation_id"],
                            "project_id": row["project_id"],
                            "sequence_no": row["sequence_no"],
                            "role": row["role"],
                            "title": title,
                            "path": f"{path} / {title}" if path else title,
                            "summary": search_repo.summarise(row["content"], query),
                            "rank": row["rank"],
                        }
                    )

        results.sort(key=lambda item: item["rank"])
        return {"query": query, "results": results[:limit], "total_kinds": counts}


def _path_cache(conn) -> dict[str, str]:
    """`id -> "root / child / grandchild"` for every live project, in one pass."""
    rows = {row["id"]: row for row in projects_repo.list_live(conn)}
    paths: dict[str, str] = {}

    def resolve(project_id: str, depth: int = 0) -> str:
        if project_id in paths:
            return paths[project_id]
        row = rows.get(project_id)
        if row is None or depth > projects_repo.MAX_DEPTH:
            return ""
        parent = row["parent_id"]
        prefix = resolve(parent, depth + 1) if parent else ""
        paths[project_id] = f"{prefix} / {row['name']}" if prefix else row["name"]
        return paths[project_id]

    for project_id in rows:
        resolve(project_id)
    return paths
