"""Attachment bytes: chunked upload, hashing, deduplication, access, retention.

Attachments live in SQLite, so the things that would normally be filesystem
problems — a partial write, a shared file deleted from under another reference,
a file nobody may read being reachable by id — are all database problems here.
"""

from __future__ import annotations

import hashlib
import os

from server_support import banner, expect_error, make_admin, make_services, make_user

from tree_agent.server.repositories import attachments as attachments_repo
from tree_agent.server.services.attachments import CHUNK_SIZE, MAX_BYTES, safe_file_name

services = make_services()
home = services.home
admin = make_admin(services)
tree = services.tree
messages = services.messages
files = services.attachments

project = tree.create_project(admin, parent_id=None, name="附件測試")
conversation = tree.create_conversation(admin, project_id=project["id"], name="對話")
message = messages.append(admin, conversation["id"], role="user", content="附件在此")


def payload(size: int, seed: bytes = b"tree-agent") -> bytes:
    """Deterministic, incompressible-ish bytes of an exact size."""
    block = hashlib.sha256(seed).digest()
    out = bytearray()
    while len(out) < size:
        block = hashlib.sha256(block).digest()
        out.extend(block)
    return bytes(out[:size])


banner("a 20 MiB attachment survives upload, hashing and download")
big = payload(MAX_BYTES)
expected_hash = hashlib.sha256(big).hexdigest()
stored = files.upload_whole(
    admin,
    conversation_id=conversation["id"],
    file_name="大檔案.bin",
    mime_type="application/octet-stream",
    data=big,
    message_id=message["id"],
)
print("stored:", stored["byte_size"], "bytes in", stored["chunk_count"], "chunks")
assert stored["byte_size"] == MAX_BYTES
assert stored["sha256"] == expected_hash
assert stored["chunk_count"] == MAX_BYTES // CHUNK_SIZE

meta, chunks = files.stream(admin, stored["id"])
digest, total, peak = hashlib.sha256(), 0, 0
for chunk in chunks:
    digest.update(chunk)
    total += len(chunk)
    peak = max(peak, len(chunk))
assert (digest.hexdigest(), total) == (expected_hash, MAX_BYTES)
assert peak <= CHUNK_SIZE, peak
print("downloaded and re-hashed; largest slice held in memory:", peak, "bytes")

banner("one byte more is refused")
expect_error(
    413,
    files.initiate,
    admin,
    conversation_id=conversation["id"],
    file_name="太大.bin",
    mime_type="application/octet-stream",
    byte_size=MAX_BYTES + 1,
)

banner("the same bytes are stored once, however many messages point at them")
second_message = messages.append(admin, conversation["id"], role="user", content="同一個檔案")
again = files.upload_whole(
    admin,
    conversation_id=conversation["id"],
    file_name="改了名字.bin",
    mime_type="application/octet-stream",
    data=big,
    message_id=second_message["id"],
)
assert again["deduplicated"] is True
assert again["id"] == stored["id"]
with services.db.read() as conn:
    rows = conn.execute("SELECT count(*) FROM attachments").fetchone()[0]
    blocks = conn.execute("SELECT count(*) FROM attachment_chunks").fetchone()[0]
print("attachment rows:", rows, "chunk rows:", blocks)
assert rows == 1 and blocks == MAX_BYTES // CHUNK_SIZE

banner("an incomplete upload cannot be committed, and its bytes stay invisible")
started = files.initiate(
    admin,
    conversation_id=conversation["id"],
    file_name="半個檔案.bin",
    mime_type="application/octet-stream",
    byte_size=3 * CHUNK_SIZE,
)
files.put_chunk(admin, started["upload_id"], 0, payload(CHUNK_SIZE, b"a"))
files.put_chunk(admin, started["upload_id"], 2, payload(CHUNK_SIZE, b"c"))
expect_error(400, files.commit, admin, started["upload_id"])
with services.db.read() as conn:
    staged = conn.execute("SELECT count(*) FROM attachment_upload_chunks").fetchone()[0]
    live = conn.execute("SELECT count(*) FROM attachments").fetchone()[0]
print("staged chunks waiting:", staged, "| attachments visible:", live)
assert staged == 2 and live == 1

banner("a declared hash that does not match the bytes is refused")
mismatch = files.initiate(
    admin,
    conversation_id=conversation["id"],
    file_name="說謊.bin",
    mime_type="application/octet-stream",
    byte_size=16,
    sha256="0" * 64,
)
files.put_chunk(admin, mismatch["upload_id"], 0, b"sixteen bytes!!!")
expect_error(400, files.commit, admin, mismatch["upload_id"])
print("SHA-256 mismatch rejected at commit")

banner("abandoned uploads are swept away")
# Age the pending uploads past their TTL rather than waiting 24 hours for it.
services.db.write(
    lambda conn: conn.execute(
        "UPDATE attachment_uploads SET expires_at_ms = 0 WHERE status = 'uploading'"
    )
)
swept = services.maintenance.sweep()
print("sweep:", swept)
with services.db.read() as conn:
    staged = conn.execute("SELECT count(*) FROM attachment_upload_chunks").fetchone()[0]
    expired = conn.execute(
        "SELECT count(*) FROM attachment_uploads WHERE status = 'expired'"
    ).fetchone()[0]
assert staged == 0 and expired >= 2, (staged, expired)

banner("holding an attachment id proves nothing")
outsider = make_user(services, admin, "dave", role="member")
expect_error(404, files.metadata, outsider, stored["id"])
# A leaked id must not become an import capability: linking it into an
# unrelated project would otherwise make private bytes downloadable.
outsider_project = tree.create_project(admin, parent_id=None, name="Dave 的專案")
tree.grant(admin, outsider_project["id"], user_id=outsider.id, permission="editor")
outsider_conversation = tree.create_conversation(
    admin, project_id=outsider_project["id"], name="Dave 的對話"
)
expect_error(
    404,
    messages.append,
    outsider,
    outsider_conversation["id"],
    role="user",
    content="嘗試偷接附件",
    attachment_ids=[stored["id"]],
)
tree.grant(admin, project["id"], user_id=outsider.id, permission="viewer")
assert files.metadata(outsider, stored["id"])["sha256"] == expected_hash
print("access follows the conversation, not the attachment id")

banner("detaching removes the link, never the bytes another message still uses")
files.detach(admin, second_message["id"], stored["id"])
with services.db.read() as conn:
    assert attachments_repo.is_live_referenced(conn, stored["id"])
assert files.metadata(admin, stored["id"])["byte_size"] == MAX_BYTES
print("bytes survive while the first message still references them")

banner("a soft-deleted conversation hides its attachments but keeps them restorable")
tree.delete_conversation(
    admin, conversation["id"], revision=tree.get_conversation(admin, conversation["id"])["revision"]
)
expect_error(404, files.metadata, admin, stored["id"])
with services.db.read() as conn:
    assert not attachments_repo.is_live_referenced(conn, stored["id"])
tree.restore_conversation(admin, conversation["id"])
assert files.metadata(admin, stored["id"])["byte_size"] == MAX_BYTES
print("restore brought the attachment back with its message")

banner("everything is still there after the service restarts")
db_path = services.config.db_path
services.close()
services = make_services(home)
reopened = services.attachments.metadata(admin, stored["id"])
assert reopened["sha256"] == expected_hash
_, chunks = services.attachments.stream(admin, stored["id"])
digest = hashlib.sha256()
for chunk in chunks:
    digest.update(chunk)
assert digest.hexdigest() == expected_hash
print("database at", os.path.basename(db_path), "reopened with the attachment intact")

banner("a file name cannot escape its own field")
assert safe_file_name(r"..\..\Windows\System32\evil.dll") == "evil.dll"
assert safe_file_name("報告\r\n: injected.pdf") == "報告: injected.pdf"
assert safe_file_name("   ") == "attachment"
print("path separators and control characters stripped from stored file names")

services.close()
print("\ntest_server_attachments OK")
