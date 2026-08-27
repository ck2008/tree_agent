"""Retention: what a purge destroys, and everything it must leave alone.

Soft delete is only half a promise. The other half is that the permanent sweep
takes exactly the rows that are both past their retention window and unreferenced
— nothing that is still reachable, and nothing that was deleted yesterday.
"""

from __future__ import annotations

import hashlib

from server_support import banner, expect_error, make_admin, make_services, make_user

from tree_agent.server.services.maintenance import DAY_MS, MIN_RETENTION_DAYS

services = make_services()
admin = make_admin(services)
member = make_user(services, admin, "alice")
tree = services.tree
messages = services.messages
files = services.attachments
maintenance = services.maintenance


def age_everything(days: int) -> None:
    """Backdate every soft delete, so retention can be tested in one second."""
    shift = days * DAY_MS

    def job(conn):
        for table in ("projects", "conversations", "messages"):
            conn.execute(
                f"UPDATE {table} SET deleted_at_ms = deleted_at_ms - ?"
                " WHERE deleted_at_ms IS NOT NULL",
                (shift,),
            )

    services.db.write(job)


def counts() -> dict[str, int]:
    with services.db.read() as conn:
        return {
            table: conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
            for table in (
                "projects",
                "conversations",
                "messages",
                "tool_calls",
                "message_attachments",
                "attachments",
                "attachment_chunks",
                "project_memberships",
            )
        }


banner("a workspace with something worth keeping and something worth losing")
keep = tree.create_project(admin, parent_id=None, name="留下的專案")
keep_conv = tree.create_conversation(admin, project_id=keep["id"], name="留下的對話")
keep_message = messages.append(admin, keep_conv["id"], role="user", content="這則要留著")

doomed = tree.create_project(admin, parent_id=None, name="要清掉的專案")
inner = tree.create_project(admin, parent_id=doomed["id"], name="內層")
doomed_conv = tree.create_conversation(admin, project_id=inner["id"], name="要清掉的對話")
doomed_message = messages.append(admin, doomed_conv["id"], role="user", content="這則要消失")
messages.add_tool_call(admin, doomed_message["id"], tool_name="shell", output_text="dir")

shared_bytes = b"shared attachment bytes" * 100
shared_hash = hashlib.sha256(shared_bytes).hexdigest()
lonely_bytes = b"only the doomed message has these" * 100

shared = files.upload_whole(
    admin,
    conversation_id=doomed_conv["id"],
    file_name="共用.bin",
    mime_type="application/octet-stream",
    data=shared_bytes,
    message_id=doomed_message["id"],
)
files.upload_whole(
    admin,
    conversation_id=keep_conv["id"],
    file_name="共用.bin",
    mime_type="application/octet-stream",
    data=shared_bytes,
    message_id=keep_message["id"],
)
lonely = files.upload_whole(
    admin,
    conversation_id=doomed_conv["id"],
    file_name="只有這裡用.bin",
    mime_type="application/octet-stream",
    data=lonely_bytes,
    message_id=doomed_message["id"],
)

fork = tree.fork_conversation(admin, doomed_conv["id"])
assert fork["forked_from_conversation_id"] == doomed_conv["id"]
before = counts()
print("before:", before)

banner("only an admin may purge, and never inside the retention window")
expect_error(403, maintenance.purge_deleted, member)
expect_error(400, maintenance.purge_deleted, admin, retention_days=MIN_RETENTION_DAYS - 1)

banner("a purge with nothing expired removes nothing")
tree.delete_project(admin, doomed["id"], revision=tree.get_project(admin, doomed["id"])["revision"])
untouched = maintenance.purge_deleted(admin, dry_run=False)
print("purge before the window elapsed:", {k: untouched[k] for k in ("projects", "conversations", "messages")})
assert (untouched["projects"], untouched["conversations"], untouched["messages"]) == (0, 0, 0)
assert counts() == before
assert tree.restore_project(admin, doomed["id"])["projects"] == 2
print("still restorable")

banner("past the window, a dry run reports without destroying")
tree.delete_project(admin, doomed["id"], revision=tree.get_project(admin, doomed["id"])["revision"])
age_everything(MIN_RETENTION_DAYS + 1)
preview = maintenance.purge_deleted(admin, dry_run=True)
print("dry run:", {k: preview[k] for k in ("projects", "conversations", "messages", "attachments")})
assert preview["dry_run"] is True
assert preview["projects"] == 2 and preview["conversations"] == 2  # the fork went with them
assert counts() == before

banner("the real purge")
result = maintenance.purge_deleted(admin, dry_run=False)
print("purged:", {k: result[k] for k in ("projects", "conversations", "messages", "attachments")})
after = counts()
print("after:", after)

assert after["projects"] == before["projects"] - 2
assert after["conversations"] == before["conversations"] - 2
assert after["tool_calls"] == 0
assert after["project_memberships"] < before["project_memberships"]

banner("what survived is exactly what is still reachable")
assert tree.get_project(admin, keep["id"])["name"] == "留下的專案"
assert messages.list_messages(admin, keep_conv["id"])["messages"][0]["content"] == "這則要留著"
expect_error(404, tree.get_project, admin, doomed["id"])
expect_error(404, tree.get_conversation, admin, doomed_conv["id"])

banner("shared attachment bytes stay, bytes nobody references go")
kept_attachment = files.metadata(admin, shared["id"])
assert kept_attachment["sha256"] == shared_hash
_, chunks = files.stream(admin, shared["id"])
assert hashlib.sha256(b"".join(chunks)).hexdigest() == shared_hash
with services.db.read() as conn:
    assert conn.execute("SELECT count(*) FROM attachments WHERE id = ?", (lonely["id"],)).fetchone()[0] == 0
    assert conn.execute(
        "SELECT count(*) FROM attachment_chunks WHERE attachment_id = ?", (lonely["id"],)
    ).fetchone()[0] == 0
assert result["attachments"] == 1, result
print("the deduplicated attachment survived on its other reference; the orphan did not")

banner("nothing dangles: every foreign key still resolves")
with services.db.read() as conn:
    violations = conn.execute("PRAGMA foreign_key_check").fetchall()
    orphan_chunks = conn.execute(
        "SELECT count(*) FROM attachment_chunks WHERE attachment_id NOT IN"
        " (SELECT id FROM attachments)"
    ).fetchone()[0]
assert not violations, violations
assert orphan_chunks == 0
assert services.db.integrity_check() == "ok"
print("foreign_key_check clean, integrity_check ok")

banner("search and the tree agree with the database")
assert services.search.search(admin, "要消失")["results"] == []
assert services.search.search(admin, "要留著")["results"]
assert [p["name"] for p in tree.tree(admin)["projects"]] == ["留下的專案"]

banner("a fork that outlives its source keeps the provenance, not the dead pointer")
survivor = tree.create_project(admin, parent_id=None, name="分岔保留")
origin = tree.create_conversation(admin, project_id=survivor["id"], name="來源對話")
tree.set_runner_state(admin, origin["id"], codex_thread_id="thread-xyz")
messages.append(admin, origin["id"], role="user", content="原始內容")
child = tree.fork_conversation(admin, origin["id"])
tree.delete_conversation(admin, origin["id"], revision=tree.get_conversation(admin, origin["id"])["revision"])
age_everything(MIN_RETENTION_DAYS + 1)
maintenance.purge_deleted(admin, dry_run=False)
detached = tree.get_conversation(admin, child["id"])
print("fork after its source was purged:", detached["forked_from_conversation_id"], "/", detached["forked_from_external_session_id"])
assert detached["forked_from_conversation_id"] is None
assert detached["forked_from_external_session_id"] == "thread-xyz"
assert messages.list_messages(admin, child["id"])["messages"][0]["content"] == "原始內容"
with services.db.read() as conn:
    assert not conn.execute("PRAGMA foreign_key_check").fetchall()

banner("housekeeping stats are reportable")
stats = maintenance.stats()
print("stats:", stats["counts"], "| db bytes:", stats["database_bytes"])
assert stats["counts"]["projects"] >= 1
assert stats["writer_queue_depth"] == 0

services.close()
print("\ntest_server_retention OK")
