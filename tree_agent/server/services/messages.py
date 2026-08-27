"""Transcript writes and reads: messages, runner events, tool calls, usage.

Runner events arrive fast and can arrive twice — a dropped connection makes a
client resend the tail of a turn. Every event may therefore carry an
`external_event_id`, unique per conversation, and a repeat returns the message
that already exists instead of writing a second copy.
"""

from __future__ import annotations

import sqlite3
from typing import Any

from .. import ids
from ..db import Database
from ..errors import NotFound, ValidationError
from ..repositories import attachments as attachments_repo
from ..repositories import conversations as conversations_repo
from ..repositories import messages as messages_repo
from ..repositories import projects as projects_repo
from ..repositories import search as search_repo
from .access import Actor, require_read, require_write

# The desktop app distinguishes Claude's terse per-tool-call events from real
# tool output, but the stored role set is fixed. The distinction is kept in
# metadata so the transcript and the info rail can still be told apart.
ROLE_ALIASES = {"agent_tool": ("tool", "agent_tool")}

USAGE_KEYS = (
    "input_tokens",
    "cached_input_tokens",
    "output_tokens",
    "reasoning_output_tokens",
)

# Roles whose text is worth putting in the search index. Tool output is
# deliberately excluded (spec §7): it is bulky, machine-generated, and would
# drown real answers in the results.
INDEXED_ROLES = ("user", "agent", "reasoning", "error", "notice", "meta")


def normalise_role(role: str) -> tuple[str, str | None]:
    if role in ROLE_ALIASES:
        return ROLE_ALIASES[role]
    if role not in messages_repo.ROLES:
        raise ValidationError(f"未知的訊息角色：{role}")
    return role, None


class MessageService:
    def __init__(self, db: Database) -> None:
        self.db = db

    # ------------------------------------------------------------- reading

    def list_messages(
        self,
        actor: Actor,
        conversation_id: str,
        *,
        after_sequence_no: int | None = None,
        before_sequence_no: int | None = None,
        limit: int = 200,
    ) -> dict[str, Any]:
        with self.db.read() as conn:
            conversation = self._conversation(conn, actor, conversation_id)
            require_read(conn, actor, conversation["project_id"])
            rows = messages_repo.list_page(
                conn,
                conversation_id,
                after_sequence_no=after_sequence_no,
                before_sequence_no=before_sequence_no,
                limit=limit,
            )
            messages = [messages_repo.row_to_message(row) for row in rows]
            message_ids = [message["id"] for message in messages]
            by_id = {message["id"]: message for message in messages}
            for message in messages:
                message["attachments"] = []
                message["tool_calls"] = []
            for row in attachments_repo.for_messages(conn, message_ids):
                item = attachments_repo.row_to_attachment(row)
                item["display_order"] = row["display_order"]
                by_id[row["message_id"]]["attachments"].append(item)
            for row in messages_repo.tool_calls_for(conn, message_ids):
                by_id[row["message_id"]]["tool_calls"].append(
                    messages_repo.row_to_tool_call(row)
                )
            return {
                "conversation_id": conversation_id,
                "messages": messages,
                "has_more": len(rows) == max(1, min(limit, 1000)),
                "next_after_sequence_no": messages[-1]["sequence_no"] if messages else None,
            }

    def usage_for(self, actor: Actor, node_id: str) -> dict[str, int]:
        """Token totals for a conversation, or for a project's whole subtree.

        Per-turn counts live in the completing message's metadata, so the total
        is a query rather than a counter that can drift out of step with the
        transcript it describes.
        """
        with self.db.read() as conn:
            conversation = conversations_repo.get(conn, node_id)
            if conversation is not None:
                require_read(conn, actor, conversation["project_id"])
                conversation_ids = [node_id]
            else:
                require_read(conn, actor, node_id)
                project_ids = projects_repo.descendant_ids(conn, node_id)
                marks = ",".join("?" * len(project_ids))
                conversation_ids = [
                    row["id"]
                    for row in conn.execute(
                        f"SELECT id FROM conversations WHERE project_id IN ({marks})"
                        " AND deleted_at_ms IS NULL",
                        project_ids,
                    )
                ]
            if not conversation_ids:
                return {}
            marks = ",".join("?" * len(conversation_ids))
            sums = ", ".join(
                f"COALESCE(SUM(json_extract(metadata_json, '$.usage.{key}')), 0) AS {key}"
                for key in USAGE_KEYS
            )
            row = conn.execute(
                f"SELECT count(*) AS turns, {sums} FROM messages"
                f" WHERE conversation_id IN ({marks}) AND deleted_at_ms IS NULL"
                " AND json_extract(metadata_json, '$.usage') IS NOT NULL",
                conversation_ids,
            ).fetchone()
            total = {"turns": row["turns"]}
            total.update({key: row[key] for key in USAGE_KEYS})
            return total if total["turns"] else {}

    # ------------------------------------------------------------- writing

    def append(
        self,
        actor: Actor,
        conversation_id: str,
        *,
        role: str,
        content: str = "",
        content_format: str = "plain",
        agent_id: str | None = None,
        model: str | None = None,
        external_event_id: str | None = None,
        metadata: dict[str, Any] | None = None,
        attachment_ids: list[str] | None = None,
        completed: bool = False,
    ) -> dict[str, Any]:
        stored_role, channel = normalise_role(role)
        if content_format not in messages_repo.CONTENT_FORMATS:
            raise ValidationError(f"未知的內容格式：{content_format}")
        metadata = dict(metadata or {})
        if channel:
            metadata.setdefault("channel", channel)

        def job(conn: sqlite3.Connection) -> dict[str, Any]:
            conversation = self._conversation(conn, actor, conversation_id)
            require_write(conn, actor, conversation["project_id"])

            if external_event_id:
                existing = messages_repo.by_external_event(
                    conn, conversation_id, external_event_id
                )
                if existing is not None:
                    # A retried event, not a new one.
                    return messages_repo.row_to_message(existing)

            now = ids.now_ms()
            message_id, sequence_no = messages_repo.insert(
                conn,
                conversation_id=conversation_id,
                role=stored_role,
                content=content,
                content_format=content_format,
                agent_id=agent_id or conversation["agent_id"],
                model=model,
                external_event_id=external_event_id,
                metadata=metadata,
                created_by=actor.id,
                now_ms=now,
                completed_at_ms=now if completed else None,
            )
            for position, attachment_id in enumerate(attachment_ids or []):
                if attachments_repo.get(conn, attachment_id) is None:
                    raise NotFound(f"找不到附件 {attachment_id}")
                # An attachment id is not a capability.  Hash de-duplication
                # deliberately lets the same bytes appear in unrelated
                # projects, so only link an existing attachment when the
                # caller can already read it through a live message.  Without
                # this check, a guessed/leaked id could be re-attached to a
                # project the caller controls and subsequently downloaded.
                reachable = attachments_repo.readable_by_message(conn, attachment_id)
                if not reachable:
                    raise NotFound(f"找不到附件 {attachment_id}")
                readable = False
                for reference in reachable:
                    try:
                        require_read(conn, actor, reference["project_id"])
                        readable = True
                        break
                    except NotFound:
                        continue
                if not readable:
                    # Keep the no-object-enumeration contract used by the
                    # attachment download endpoints.
                    raise NotFound(f"找不到附件 {attachment_id}")
                attachments_repo.link(conn, message_id, attachment_id, position)
            conversations_repo.touch(conn, conversation_id, now)
            if stored_role in INDEXED_ROLES:
                search_repo.index_message(
                    conn, message_id, conversation_id, conversation["project_id"], content
                )
            return messages_repo.row_to_message(messages_repo.get(conn, message_id))

        return self.db.write(job, label="append_message")

    def append_delta(self, actor: Actor, message_id: str, delta: str) -> dict[str, Any]:
        """Grow a streaming message without rewriting what is already stored."""

        def job(conn: sqlite3.Connection) -> dict[str, Any]:
            row = messages_repo.get(conn, message_id)
            if row is None:
                raise NotFound("找不到訊息")
            conversation = self._conversation(conn, actor, row["conversation_id"])
            require_write(conn, actor, conversation["project_id"])
            now = ids.now_ms()
            messages_repo.append_content(conn, message_id, delta, now_ms=now)
            conversations_repo.touch(conn, row["conversation_id"], now)
            return {"id": message_id, "appended": len(delta)}

        return self.db.write(job, label="append_delta")

    def complete(
        self,
        actor: Actor,
        message_id: str,
        *,
        content: str | None = None,
        usage: dict[str, Any] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Close a streamed message and file the turn's token counts with it."""

        def job(conn: sqlite3.Connection) -> dict[str, Any]:
            row = messages_repo.get(conn, message_id)
            if row is None:
                raise NotFound("找不到訊息")
            conversation = self._conversation(conn, actor, row["conversation_id"])
            require_write(conn, actor, conversation["project_id"])

            merged = messages_repo.row_to_message(row)["metadata"]
            merged.update(metadata or {})
            if usage:
                merged["usage"] = {
                    key: int(usage[key]) for key in USAGE_KEYS if isinstance(usage.get(key), int)
                }
            now = ids.now_ms()
            messages_repo.complete(conn, message_id, now_ms=now, content=content, metadata=merged)
            conversations_repo.touch(conn, row["conversation_id"], now)
            updated = messages_repo.get(conn, message_id)
            if updated["role"] in INDEXED_ROLES:
                search_repo.index_message(
                    conn,
                    message_id,
                    row["conversation_id"],
                    conversation["project_id"],
                    updated["content"],
                )
            return messages_repo.row_to_message(updated)

        return self.db.write(job, label="complete_message")

    def add_tool_call(
        self,
        actor: Actor,
        message_id: str,
        *,
        tool_name: str,
        status: str = "running",
        payload: dict[str, Any] | None = None,
        output_text: str = "",
        error_text: str | None = None,
    ) -> dict[str, Any]:
        if status not in messages_repo.TOOL_STATUSES:
            raise ValidationError(f"未知的工具狀態：{status}")

        def job(conn: sqlite3.Connection) -> dict[str, Any]:
            row = messages_repo.get(conn, message_id)
            if row is None:
                raise NotFound("找不到訊息")
            conversation = self._conversation(conn, actor, row["conversation_id"])
            require_write(conn, actor, conversation["project_id"])
            now = ids.now_ms()
            tool_call_id = messages_repo.add_tool_call(
                conn,
                message_id=message_id,
                tool_name=tool_name,
                status=status,
                payload=payload,
                output_text=output_text,
                error_text=error_text,
                started_at_ms=now,
                completed_at_ms=now if status in ("completed", "failed", "cancelled") else None,
            )
            return messages_repo.row_to_tool_call(
                conn.execute("SELECT * FROM tool_calls WHERE id = ?", (tool_call_id,)).fetchone()
            )

        return self.db.write(job, label="add_tool_call")

    def update_tool_call(
        self,
        actor: Actor,
        tool_call_id: str,
        *,
        status: str | None = None,
        output_text: str | None = None,
        error_text: str | None = None,
    ) -> dict[str, Any]:
        if status is not None and status not in messages_repo.TOOL_STATUSES:
            raise ValidationError(f"未知的工具狀態：{status}")

        def job(conn: sqlite3.Connection) -> dict[str, Any]:
            row = conn.execute(
                "SELECT t.*, m.conversation_id FROM tool_calls t"
                " JOIN messages m ON m.id = t.message_id WHERE t.id = ?",
                (tool_call_id,),
            ).fetchone()
            if row is None:
                raise NotFound("找不到工具呼叫")
            conversation = self._conversation(conn, actor, row["conversation_id"])
            require_write(conn, actor, conversation["project_id"])
            fields: dict[str, Any] = {}
            if status is not None:
                fields["status"] = status
                if status in ("completed", "failed", "cancelled"):
                    fields["completed_at_ms"] = ids.now_ms()
            if output_text is not None:
                fields["output_text"] = output_text
            if error_text is not None:
                fields["error_text"] = error_text
            messages_repo.update_tool_call(conn, tool_call_id, fields)
            return messages_repo.row_to_tool_call(
                conn.execute("SELECT * FROM tool_calls WHERE id = ?", (tool_call_id,)).fetchone()
            )

        return self.db.write(job, label="update_tool_call")

    def cancel_turn(self, actor: Actor, conversation_id: str, note: str = "（已停止）") -> dict[str, Any]:
        """Mark anything still in flight as cancelled and say so in the transcript."""

        def job(conn: sqlite3.Connection) -> dict[str, Any]:
            conversation = self._conversation(conn, actor, conversation_id)
            require_write(conn, actor, conversation["project_id"])
            now = ids.now_ms()
            cancelled = conn.execute(
                "UPDATE tool_calls SET status = 'cancelled', completed_at_ms = ?"
                " WHERE status IN ('pending','running') AND message_id IN ("
                "   SELECT id FROM messages WHERE conversation_id = ?"
                "   AND deleted_at_ms IS NULL)",
                (now, conversation_id),
            ).rowcount
            open_messages = conn.execute(
                "UPDATE messages SET completed_at_ms = ? WHERE conversation_id = ?"
                " AND completed_at_ms IS NULL AND deleted_at_ms IS NULL",
                (now, conversation_id),
            ).rowcount
            message_id, _ = messages_repo.insert(
                conn,
                conversation_id=conversation_id,
                role="meta",
                content=note,
                now_ms=now,
                created_by=actor.id,
                completed_at_ms=now,
            )
            search_repo.index_message(
                conn, message_id, conversation_id, conversation["project_id"], note
            )
            conversations_repo.touch(conn, conversation_id, now)
            return {
                "cancelled_tool_calls": cancelled,
                "closed_messages": open_messages,
                "message_id": message_id,
            }

        return self.db.write(job, label="cancel_turn")

    def delete_message(self, actor: Actor, message_id: str) -> dict[str, Any]:
        def job(conn: sqlite3.Connection) -> dict[str, Any]:
            row = messages_repo.get(conn, message_id)
            if row is None:
                raise NotFound("找不到訊息")
            conversation = self._conversation(conn, actor, row["conversation_id"])
            require_write(conn, actor, conversation["project_id"])
            now = ids.now_ms()
            messages_repo.soft_delete(conn, message_id, now)
            search_repo.drop_messages(conn, [message_id])
            return {"id": message_id, "deleted_at_ms": now}

        return self.db.write(job, label="delete_message")

    # ----------------------------------------------------------- internals

    @staticmethod
    def _conversation(
        conn: sqlite3.Connection, actor: Actor, conversation_id: str
    ) -> sqlite3.Row:
        row = conversations_repo.get(conn, conversation_id)
        if row is None:
            raise NotFound("找不到對話")
        require_read(conn, actor, row["project_id"])
        return row
