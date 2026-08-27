"""Full-text index maintenance and querying.

Two things need explaining.

**Segmentation.** `unicode61` splits on non-alphanumerics, and every CJK
ideograph counts as alphanumeric — so "我的專案筆記" is a *single* token and a
search for "專案" would never match it. Text is therefore indexed with each CJK
character as its own token, and CJK query runs become phrase queries over the
same characters. Latin text is untouched, so English and mixed queries behave
exactly as they read.

**Query building.** User input never reaches FTS5 as an expression. Every term
is emitted as a quoted string literal with embedded quotes doubled, so `AND`,
`NEAR`, `*`, `^` and friends in a search box are searched for, not executed.
"""

from __future__ import annotations

import re
import sqlite3
from typing import Any, Iterable

# Ranges that need per-character tokens: CJK ideographs and their extensions,
# plus the Japanese kana and Hangul syllables that share the same problem.
_CJK_PATTERN = (
    r"⺀-⿿぀-ヿ㐀-䶿一-鿿"
    r"豈-﫿가-힯ｦ-ﾟ"
)
_CJK_RUN = re.compile(f"[{_CJK_PATTERN}]+")
_IS_CJK = re.compile(f"^[{_CJK_PATTERN}]+$")
_TERM = re.compile(f"[{_CJK_PATTERN}]+|[^\\s{_CJK_PATTERN}]+")

# Only the first slice of a long tool dump or transcript is worth indexing; the
# rest inflates the index for matches nobody scrolls to.
MAX_INDEXED_CHARS = 200_000


def segment(text: str | None) -> str:
    """Space out CJK characters so each becomes its own FTS token."""
    if not text:
        return ""
    text = text[:MAX_INDEXED_CHARS]
    return _CJK_RUN.sub(lambda m: " ".join(m.group(0)), text)


def build_match(query: str) -> str:
    """A safe FTS5 MATCH expression, or "" when there is nothing to search for."""
    parts: list[str] = []
    for term in _TERM.findall(query or ""):
        term = term.strip("\"'`()[]{}*^:-")
        if not term:
            continue
        if _IS_CJK.match(term):
            # A phrase over the individual characters == a substring match.
            body = " ".join(term)
        else:
            body = term
        parts.append('"' + body.replace('"', '""') + '"')
    return " ".join(parts)


# ------------------------------------------------------------- maintenance
#
# Every one of these runs in the same transaction as the row it describes.
# Delete-then-insert on the unique id keeps the index a faithful mirror; FTS5
# has no primary key of its own, and `rowid` must never be treated as one.


def index_project(conn: sqlite3.Connection, project_id: str, name: str, prompt: str | None) -> None:
    conn.execute("DELETE FROM project_fts WHERE project_id = ?", (project_id,))
    conn.execute(
        "INSERT INTO project_fts (project_id, name, prompt) VALUES (?, ?, ?)",
        (project_id, segment(name), segment(prompt)),
    )


def drop_projects(conn: sqlite3.Connection, project_ids: Iterable[str]) -> None:
    for project_id in project_ids:
        conn.execute("DELETE FROM project_fts WHERE project_id = ?", (project_id,))


def index_conversation(
    conn: sqlite3.Connection, conversation_id: str, project_id: str, name: str
) -> None:
    conn.execute(
        "DELETE FROM conversation_fts WHERE conversation_id = ?", (conversation_id,)
    )
    conn.execute(
        "INSERT INTO conversation_fts (conversation_id, project_id, name)"
        " VALUES (?, ?, ?)",
        (conversation_id, project_id, segment(name)),
    )


def drop_conversations(conn: sqlite3.Connection, conversation_ids: Iterable[str]) -> None:
    for conversation_id in conversation_ids:
        conn.execute(
            "DELETE FROM conversation_fts WHERE conversation_id = ?", (conversation_id,)
        )


def index_message(
    conn: sqlite3.Connection,
    message_id: str,
    conversation_id: str,
    project_id: str,
    content: str,
) -> None:
    conn.execute("DELETE FROM message_fts WHERE message_id = ?", (message_id,))
    if not (content or "").strip():
        return
    conn.execute(
        "INSERT INTO message_fts (message_id, conversation_id, project_id, content)"
        " VALUES (?, ?, ?, ?)",
        (message_id, conversation_id, project_id, segment(content)),
    )


def drop_messages(conn: sqlite3.Connection, message_ids: Iterable[str]) -> None:
    for message_id in message_ids:
        conn.execute("DELETE FROM message_fts WHERE message_id = ?", (message_id,))


def reindex_conversation(
    conn: sqlite3.Connection, conversation_id: str, project_id: str, name: str
) -> None:
    """Re-stamp a conversation and all its live messages with a new project id.

    This is why the indexes are not maintained by triggers: after a move, the
    `project_id` stored against every message under the conversation is stale,
    and permission filtering during search relies on it being right.
    """
    index_conversation(conn, conversation_id, project_id, name)
    rows = conn.execute(
        "SELECT id, content FROM messages WHERE conversation_id = ?"
        " AND deleted_at_ms IS NULL",
        (conversation_id,),
    ).fetchall()
    for row in rows:
        index_message(conn, row["id"], conversation_id, project_id, row["content"])


# ----------------------------------------------------------------- querying


def _project_filter(project_ids: list[str]) -> tuple[str, list[Any]]:
    marks = ",".join("?" * len(project_ids))
    return f" AND f.project_id IN ({marks})", list(project_ids)


def search_projects(
    conn: sqlite3.Connection, match: str, project_ids: list[str], limit: int, offset: int
) -> list[sqlite3.Row]:
    if not project_ids:
        return []
    marks = ",".join("?" * len(project_ids))
    return conn.execute(
        "SELECT f.project_id, p.name, p.prompt, bm25(project_fts) AS rank"
        " FROM project_fts f JOIN projects p ON p.id = f.project_id"
        " WHERE project_fts MATCH ? AND p.deleted_at_ms IS NULL"
        f" AND f.project_id IN ({marks})"
        " ORDER BY rank LIMIT ? OFFSET ?",
        [match, *project_ids, limit, offset],
    ).fetchall()


def search_conversations(
    conn: sqlite3.Connection, match: str, project_ids: list[str], limit: int, offset: int
) -> list[sqlite3.Row]:
    if not project_ids:
        return []
    clause, params = _project_filter(project_ids)
    return conn.execute(
        "SELECT f.conversation_id, f.project_id, c.name, bm25(conversation_fts) AS rank"
        " FROM conversation_fts f JOIN conversations c ON c.id = f.conversation_id"
        " WHERE conversation_fts MATCH ? AND c.deleted_at_ms IS NULL"
        + clause
        + " ORDER BY rank LIMIT ? OFFSET ?",
        [match, *params, limit, offset],
    ).fetchall()


def search_messages(
    conn: sqlite3.Connection, match: str, project_ids: list[str], limit: int, offset: int
) -> list[sqlite3.Row]:
    if not project_ids:
        return []
    clause, params = _project_filter(project_ids)
    return conn.execute(
        "SELECT f.message_id, f.conversation_id, f.project_id, m.content, m.role,"
        " m.sequence_no, c.name AS conversation_name, bm25(message_fts) AS rank"
        " FROM message_fts f"
        " JOIN messages m ON m.id = f.message_id"
        " JOIN conversations c ON c.id = m.conversation_id"
        " WHERE message_fts MATCH ? AND m.deleted_at_ms IS NULL"
        " AND c.deleted_at_ms IS NULL"
        + clause
        + " ORDER BY rank LIMIT ? OFFSET ?",
        [match, *params, limit, offset],
    ).fetchall()


def summarise(content: str, query: str, width: int = 120) -> str:
    """A readable excerpt around the first matching term.

    Built from the stored text rather than FTS5's `snippet()`, which would hand
    back the space-separated CJK form the index holds.
    """
    content = (content or "").replace("\n", " ").strip()
    if len(content) <= width:
        return content
    terms = [t for t in _TERM.findall(query or "") if t.strip()]
    lowered = content.lower()
    position = -1
    for term in terms:
        position = lowered.find(term.lower())
        if position >= 0:
            break
    if position < 0:
        return content[:width].rstrip() + "…"
    start = max(0, position - width // 3)
    excerpt = content[start : start + width].strip()
    return ("…" if start else "") + excerpt + ("…" if start + width < len(content) else "")
