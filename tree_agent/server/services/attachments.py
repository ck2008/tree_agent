"""Chunked attachment upload, download and deduplication.

Nothing here ever holds a whole file. Uploads land one 1 MiB chunk at a time in
a staging table, the commit hashes them by streaming the same chunks back, and
downloads fetch one chunk per query so a slow client cannot pin a read
transaction open and stall WAL checkpointing.

Staged chunks are invisible to every download path. Only `commit` — which
verifies the chunk numbering, the total length and the SHA-256 in one
transaction — can turn them into a real attachment.
"""

from __future__ import annotations

import hashlib
import os
import re
import sqlite3
from typing import Any, Iterator

from .. import ids
from ..db import Database
from ..errors import ConflictError, NotFound, PayloadTooLarge, PermissionDenied, ValidationError
from ..repositories import attachments as attachments_repo
from ..repositories import conversations as conversations_repo
from ..repositories import messages as messages_repo
from .access import Actor, require_read, require_write

CHUNK_SIZE = attachments_repo.CHUNK_SIZE
MAX_BYTES = attachments_repo.MAX_BYTES

_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
_MIME = re.compile(r"^[a-zA-Z0-9!#$&^_.+-]+/[a-zA-Z0-9!#$&^_.+-]+$")
DEFAULT_MIME = "application/octet-stream"


def safe_file_name(name: str) -> str:
    """A display name that cannot escape a directory or forge a header.

    The stored name is metadata only — nothing on the server ever opens a path
    built from it — but it does end up in `Content-Disposition`, so separators
    and control characters come out.
    """
    name = _CONTROL.sub("", (name or "").strip())
    name = name.replace("\\", "/").rsplit("/", 1)[-1]
    name = name.strip(". ")
    if not name:
        name = "attachment"
    return name[:255]


def _check_mime(mime_type: str) -> str:
    mime_type = (mime_type or "").strip().lower() or DEFAULT_MIME
    if not _MIME.match(mime_type):
        raise ValidationError(f"不是合法的 MIME type：{mime_type}")
    return mime_type


class AttachmentService:
    def __init__(self, db: Database) -> None:
        self.db = db

    # ------------------------------------------------------------- uploads

    def initiate(
        self,
        actor: Actor,
        *,
        conversation_id: str,
        file_name: str,
        mime_type: str,
        byte_size: int,
        sha256: str | None = None,
        message_id: str | None = None,
    ) -> dict[str, Any]:
        if not isinstance(byte_size, int) or byte_size <= 0:
            raise ValidationError("附件大小必須大於 0")
        if byte_size > MAX_BYTES:
            raise PayloadTooLarge(
                f"單一附件最大 {MAX_BYTES // (1024 * 1024)} MiB，這個檔案是 {byte_size} bytes"
            )
        file_name = safe_file_name(file_name)
        mime_type = _check_mime(mime_type)
        if sha256 is not None and not re.fullmatch(r"[0-9a-fA-F]{64}", sha256):
            raise ValidationError("sha256 必須是 64 個十六進位字元")

        def job(conn: sqlite3.Connection) -> dict[str, Any]:
            conversation = self._conversation(conn, actor, conversation_id)
            require_write(conn, actor, conversation["project_id"])
            if message_id is not None:
                message = messages_repo.get(conn, message_id)
                if message is None or message["conversation_id"] != conversation_id:
                    raise NotFound("找不到要附加的訊息")
            upload_id = attachments_repo.create_upload(
                conn,
                user_id=actor.id,
                file_name=file_name,
                mime_type=mime_type,
                expected_byte_size=byte_size,
                expected_sha256=(sha256 or "").lower() or None,
                target_message_id=message_id,
                now_ms=ids.now_ms(),
            )
            return {
                "upload_id": upload_id,
                "chunk_size": CHUNK_SIZE,
                "chunk_count": (byte_size + CHUNK_SIZE - 1) // CHUNK_SIZE,
            }

        return self.db.write(job, label="initiate_upload")

    def put_chunk(self, actor: Actor, upload_id: str, chunk_no: int, payload: bytes) -> dict[str, Any]:
        if chunk_no < 0:
            raise ValidationError("chunk 編號從 0 開始")
        if not payload:
            raise ValidationError("chunk 不可為空")
        if len(payload) > CHUNK_SIZE:
            raise PayloadTooLarge(f"每個 chunk 最大 {CHUNK_SIZE} bytes")

        def job(conn: sqlite3.Connection) -> dict[str, Any]:
            upload = self._open_upload(conn, actor, upload_id)
            if chunk_no * CHUNK_SIZE + len(payload) > upload["expected_byte_size"]:
                raise ValidationError("上傳的內容超過一開始宣告的大小")
            attachments_repo.put_upload_chunk(conn, upload_id, chunk_no, payload)
            received = attachments_repo.get_upload(conn, upload_id)["received_byte_size"]
            return {"upload_id": upload_id, "chunk_no": chunk_no, "received_byte_size": received}

        return self.db.write(job, label="put_chunk")

    def commit(
        self, actor: Actor, upload_id: str, *, message_id: str | None = None
    ) -> dict[str, Any]:
        """Verify the staged bytes and publish them as an attachment."""

        def job(conn: sqlite3.Connection) -> dict[str, Any]:
            upload = self._open_upload(conn, actor, upload_id)
            target_message_id = message_id or upload["target_message_id"]

            numbers = attachments_repo.upload_chunk_numbers(conn, upload_id)
            expected = (upload["expected_byte_size"] + CHUNK_SIZE - 1) // CHUNK_SIZE
            if numbers != list(range(expected)):
                raise ValidationError(
                    f"chunk 不完整：預期 0..{expected - 1}，實際收到 {len(numbers)} 個"
                )

            digest = hashlib.sha256()
            total = 0
            for chunk in attachments_repo.iter_upload_chunks(conn, upload_id):
                digest.update(chunk)
                total += len(chunk)
            if total != upload["expected_byte_size"]:
                raise ValidationError(
                    f"大小不符：宣告 {upload['expected_byte_size']} bytes，實際 {total} bytes"
                )
            checksum = digest.hexdigest()
            if upload["expected_sha256"] and checksum != upload["expected_sha256"]:
                raise ValidationError("SHA-256 與宣告的值不符，請重新上傳")

            now = ids.now_ms()
            existing = attachments_repo.by_hash(conn, checksum, total)
            if existing is not None:
                attachment_id = existing["id"]
                deduplicated = True
            else:
                attachment_id = attachments_repo.create_from_upload(
                    conn,
                    upload_id=upload_id,
                    sha256=checksum,
                    file_name=upload["file_name"],
                    mime_type=upload["mime_type"],
                    byte_size=total,
                    chunk_count=len(numbers),
                    created_by=actor.id,
                    now_ms=now,
                )
                deduplicated = False

            if target_message_id:
                message = messages_repo.get(conn, target_message_id)
                if message is None:
                    raise NotFound("找不到要附加的訊息")
                conversation = self._conversation(conn, actor, message["conversation_id"])
                require_write(conn, actor, conversation["project_id"])
                attachments_repo.link(conn, target_message_id, attachment_id)

            attachments_repo.set_upload_status(
                conn, upload_id, "committed", committed_attachment_id=attachment_id
            )
            attachments_repo.drop_upload_chunks(conn, upload_id)

            result = attachments_repo.row_to_attachment(attachments_repo.get(conn, attachment_id))
            result["deduplicated"] = deduplicated
            result["message_id"] = target_message_id
            return result

        return self.db.write(job, label="commit_upload")

    def upload_whole(
        self,
        actor: Actor,
        *,
        conversation_id: str,
        file_name: str,
        mime_type: str,
        data: bytes,
        message_id: str | None = None,
    ) -> dict[str, Any]:
        """Convenience path for small files: initiate, send, commit.

        Still chunked underneath, so the 1 MiB peak holds.
        """
        started = self.initiate(
            actor,
            conversation_id=conversation_id,
            file_name=file_name,
            mime_type=mime_type,
            byte_size=len(data),
            sha256=hashlib.sha256(data).hexdigest(),
            message_id=message_id,
        )
        for index in range(0, len(data), CHUNK_SIZE):
            self.put_chunk(
                actor, started["upload_id"], index // CHUNK_SIZE, data[index : index + CHUNK_SIZE]
            )
        return self.commit(actor, started["upload_id"], message_id=message_id)

    # ----------------------------------------------------------- downloads

    def metadata(self, actor: Actor, attachment_id: str) -> dict[str, Any]:
        with self.db.read() as conn:
            row = attachments_repo.get(conn, attachment_id)
            if row is None or row["deleted_at_ms"] is not None:
                raise NotFound("找不到附件")
            self._require_reachable(conn, actor, attachment_id)
            return attachments_repo.row_to_attachment(row)

    def stream(self, actor: Actor, attachment_id: str) -> tuple[dict[str, Any], Iterator[bytes]]:
        """Metadata plus a generator over the bytes, one chunk per query.

        Deliberately not one long read transaction: a client on a slow link
        would otherwise keep a snapshot alive for the whole transfer.
        """
        meta = self.metadata(actor, attachment_id)

        def chunks() -> Iterator[bytes]:
            for chunk_no in range(meta["chunk_count"]):
                with self.db.read() as conn:
                    row = conn.execute(
                        "SELECT bytes FROM attachment_chunks"
                        " WHERE attachment_id = ? AND chunk_no = ?",
                        (attachment_id, chunk_no),
                    ).fetchone()
                if row is None:
                    raise NotFound(f"附件 {attachment_id} 的第 {chunk_no} 段遺失")
                yield row["bytes"]

        return meta, chunks()

    def detach(self, actor: Actor, message_id: str, attachment_id: str) -> dict[str, Any]:
        def job(conn: sqlite3.Connection) -> dict[str, Any]:
            message = messages_repo.get(conn, message_id)
            if message is None:
                raise NotFound("找不到訊息")
            conversation = self._conversation(conn, actor, message["conversation_id"])
            require_write(conn, actor, conversation["project_id"])
            removed = attachments_repo.unlink(conn, message_id, attachment_id)
            # Bytes stay put: another message may share them, and a purge run
            # is the only thing allowed to delete them.
            return {"detached": bool(removed)}

        return self.db.write(job, label="detach_attachment")

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

    @staticmethod
    def _open_upload(conn: sqlite3.Connection, actor: Actor, upload_id: str) -> sqlite3.Row:
        upload = attachments_repo.get_upload(conn, upload_id)
        if upload is None:
            raise NotFound("找不到這個上傳")
        if upload["user_id"] != actor.id:
            raise PermissionDenied("這不是你的上傳")
        if upload["status"] != "uploading":
            raise ConflictError(f"這個上傳已經是 {upload['status']} 狀態")
        if upload["expires_at_ms"] <= ids.now_ms():
            raise ConflictError("這個上傳已逾時，請重新開始")
        return upload

    @staticmethod
    def _require_reachable(conn: sqlite3.Connection, actor: Actor, attachment_id: str) -> None:
        """Holding an attachment id proves nothing — the same bytes are shared.

        Access is granted through a live message in a project the caller can
        read, so deduplication can never widen anybody's reach.
        """
        for row in attachments_repo.readable_by_message(conn, attachment_id):
            try:
                require_read(conn, actor, row["project_id"])
                return
            except Exception:  # noqa: BLE001 - try the next route
                continue
        raise NotFound("找不到附件")


def sniff_mime(file_name: str, fallback: str = DEFAULT_MIME) -> str:
    """Best-effort content type from the file name, for the legacy importer."""
    import mimetypes

    guess, _ = mimetypes.guess_type(os.path.basename(file_name or ""))
    return guess or fallback
