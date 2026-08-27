"""Importing a real desktop `workspace.json` into the shared database.

The source workspace here is built with `tree_agent.store` itself rather than
hand-written JSON, so the importer is tested against the format the app actually
writes — including the parts the spec calls out: image attachments on disk, tool
output, forks, and roles this schema does not have a column for.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile

from server_support import banner, expect_error, make_admin, make_services, make_user

from tree_agent import store
from tree_agent.server.migrations.legacy_workspace_import import import_workspace
from tree_agent.server.repositories import attachments as attachments_repo

# ------------------------------------------------------------ the source

legacy_home = tempfile.mkdtemp(prefix="legacy-workspace-")
workspace = store.Workspace(legacy_home)
root = workspace.projects[0]
workspace.rename(root["id"], "舊工作區")
workspace.set_option(root["id"], "cwd", r"E:\GitHub\ck2008")
workspace.set_option(root["id"], "prompt", "回答請用繁體中文")

client = workspace.add_project(root["id"], "客戶專案")
workspace.set_option(client["id"], "sandbox", "read-only")
workspace.set_option(client["id"], "prompt", "這個客戶用 .NET Framework")
deep = workspace.add_project(client["id"], "子系統")

# Image attachments live outside the JSON, as real files.
attachment_dir = os.path.join(legacy_home, "attachments")
os.makedirs(attachment_dir, exist_ok=True)
image_paths = []
for index, colour in enumerate((b"red", b"green")):
    path = os.path.join(attachment_dir, f"screenshot-{index}.png")
    with open(path, "wb") as handle:
        handle.write(b"\x89PNG\r\n\x1a\n" + hashlib.sha256(colour).digest() * 64)
    image_paths.append(path)
# The same bytes saved under a second name: the import must store them once.
duplicate_path = os.path.join(attachment_dir, "copy-of-screenshot-0.png")
with open(duplicate_path, "wb") as handle:
    handle.write(open(image_paths[0], "rb").read())
missing_path = os.path.join(attachment_dir, "deleted-before-import.png")
source_hashes = {
    os.path.basename(p): hashlib.sha256(open(p, "rb").read()).hexdigest() for p in image_paths
}

first = workspace.add_conversation(deep["id"], "第一個對話")
workspace.set_thread_id(first["id"], "codex-thread-0001")
workspace.append_message(first["id"], "user", "看一下這兩張圖", images=list(image_paths))
workspace.append_message(first["id"], "reasoning", "先確認圖片內容")
workspace.append_message(first["id"], "tool", "ran: dir E:\\GitHub\\ck2008")
workspace.append_message(first["id"], "agent", "兩張圖都是同一頁的不同狀態。", agent_id="codex")
# Claude's terse per-tool-call event: a role this schema does not have.
workspace.append_message(first["id"], "agent_tool", "Read(store.py)")
# A role nothing has ever emitted, to prove unknown values are kept, not dropped.
workspace.append_message(first["id"], "from-the-future", "???")
# An image whose file is gone must be reported, not fatal.
workspace.append_message(first["id"], "user", "這張圖不見了", images=[missing_path])

claude = workspace.add_conversation(deep["id"], "Claude 對話")
workspace.set_conversation_agent(claude["id"], "claude")
claude_node = workspace.find(claude["id"])
claude_node["claude_session_id"] = "claude-session-42"
workspace.append_message(
    claude["id"], "user", "同一張圖再看一次", images=[image_paths[0], duplicate_path]
)

forked = workspace.fork_conversation(first["id"])
assert forked is not None and forked["fork_of"] == "codex-thread-0001"
workspace.save()

source = json.load(open(workspace.path, encoding="utf-8"))


def walk(nodes):
    """Count what the file actually holds, rather than restating it by hand."""
    counts = {"projects": 0, "conversations": 0, "messages": 0, "links": 0}
    for node in nodes:
        if node["kind"] == store.CONVERSATION:
            counts["conversations"] += 1
            for message in node["messages"]:
                counts["messages"] += 1
                # One link per message per distinct set of bytes: two names for
                # the same file attached to one message is still one attachment.
                counts["links"] += len(
                    {
                        hashlib.sha256(open(image, "rb").read()).hexdigest()
                        for image in (message.get("images") or [])
                        if os.path.isfile(image)
                    }
                )
        else:
            counts["projects"] += 1
            for key, value in walk(node["children"]).items():
                counts[key] += value
    return counts


expected = walk(source["projects"])
print("source workspace:", expected)

# ------------------------------------------------------------ the import

services = make_services()
admin = make_admin(services)
member = make_user(services, admin, "alice")

banner("only an administrator may start an import")
expect_error(403, import_workspace, services, member, source_path=workspace.path)
expect_error(404, import_workspace, services, admin, source_path=os.path.join(legacy_home, "nope.json"))

banner("a dry run reports what would happen and writes nothing")
preview = import_workspace(services, admin, source_path=workspace.path, dry_run=True)
print("dry run:", preview["summary"])
print("dry run issues:", [i["kind"] for i in preview["issues"]])
assert preview["summary"]["conversations"] == expected["conversations"]
assert {i["kind"] for i in preview["issues"]} == {"unknown_role", "missing_attachment"}
assert services.tree.tree(admin)["projects"] == []

banner("the real import")
result = import_workspace(services, admin, source_path=workspace.path)
summary = result["summary"]
print("summary:", {k: v for k, v in summary.items() if k != "attachment_ids"})
print("issues:", [(i["kind"], i["detail"][:40]) for i in result["issues"]])
assert result["status"] == "completed"
assert summary["projects"] == summary["source_counts"]["projects"] == expected["projects"]
assert summary["conversations"] == expected["conversations"]
assert summary["messages"] == summary["source_counts"]["messages"] == expected["messages"]
# Three files on disk, two distinct sets of bytes: the copy is deduplicated by
# hash rather than stored twice.
assert summary["attachments"] == 2, summary
assert summary["deduplicated_attachments"] == 1, summary
assert summary["verified_attachments"] == 2

banner("the source is backed up read-only and never modified")
backup = summary["backup_path"]
assert os.path.isfile(os.path.join(backup, "workspace.json"))
assert os.path.isdir(os.path.join(backup, "attachments"))
assert json.load(open(workspace.path, encoding="utf-8")) == source
print("backup at", os.path.basename(backup))

banner("the tree came across with its shape, settings and prompts")
tree = services.tree
imported = tree.tree(admin)["projects"]
assert [p["name"] for p in imported] == ["舊工作區"]
old = imported[0]
assert [p["name"] for p in old["children"]] == ["客戶專案"]
subsystem = old["children"][0]["children"][0]
assert subsystem["name"] == "子系統"
conversations = {c["name"]: c for c in subsystem["conversations"]}
print("conversations:", sorted(conversations))
assert set(conversations) == {"第一個對話", "Claude 對話", "第一個對話 (分岔)"}

settings = tree.resolve(admin, conversations["第一個對話"]["id"])
print("resolved settings:", settings)
assert settings["cwd"] == r"E:\GitHub\ck2008" and settings["sandbox"] == "read-only"
assert tree.instructions(admin, subsystem["id"]) == "回答請用繁體中文\n\n這個客戶用 .NET Framework"

banner("runner ids, agents and fork provenance survived")
assert conversations["第一個對話"]["codex_thread_id"] == "codex-thread-0001"
assert conversations["Claude 對話"]["agent_id"] == "claude"
assert conversations["Claude 對話"]["claude_session_id"] == "claude-session-42"
assert conversations["第一個對話 (分岔)"]["forked_from_external_session_id"] == "codex-thread-0001"

banner("messages kept their order, and unmappable roles kept their original value")
transcript = services.messages.list_messages(admin, conversations["第一個對話"]["id"])["messages"]
print("roles:", [m["role"] for m in transcript])
assert [m["sequence_no"] for m in transcript] == list(range(1, len(transcript) + 1))
assert transcript[0]["content"] == "看一下這兩張圖"
tool_event = next(m for m in transcript if m["metadata"].get("channel") == "agent_tool")
assert tool_event["role"] == "tool" and tool_event["content"] == "Read(store.py)"
unknown = next(m for m in transcript if m["metadata"].get("legacy_role"))
assert unknown["role"] == "notice" and unknown["metadata"]["legacy_role"] == "from-the-future"
assert next(m for m in transcript if m["role"] == "tool" and "dir E:" in m["content"])
print("agent_tool became a tool message; the unknown role became a notice with its original value")

banner("attachment bytes match the files on disk, and are stored once")
attached = transcript[0]["attachments"]
assert len(attached) == 2, attached
for item in attached:
    with services.db.read() as conn:
        digest = hashlib.sha256()
        for chunk in attachments_repo.iter_chunks(conn, item["id"]):
            digest.update(chunk)
    assert digest.hexdigest() == item["sha256"] == source_hashes[item["file_name"]], item
    print(" ", item["file_name"], item["byte_size"], "bytes", item["sha256"][:12])
with services.db.read() as conn:
    total = conn.execute("SELECT count(*) FROM attachments").fetchone()[0]
    links = conn.execute("SELECT count(*) FROM message_attachments").fetchone()[0]
print("attachment rows:", total, "| links:", links)
# Every image reference that pointed at a file that still exists became a link;
# the one whose file was deleted became an issue instead.
assert total == 2, total
assert links == expected["links"], (links, expected)

banner("everything the import created belongs to the admin who ran it")
memberships = tree.memberships(admin, old["id"])
assert [(m["username"], m["permission"]) for m in memberships] == [("admin", "owner")]
expect_error(404, tree.get_project, member, old["id"])

banner("the imported workspace is searchable straight away")
assert {r["title"] for r in services.search.search(admin, "兩張圖")["results"]} == {"第一個對話", "第一個對話 (分岔)"}
assert {r["kind"] for r in services.search.search(admin, "客戶")["results"]} == {"project"}

banner("a report was written")
with services.db.read() as conn:
    report = conn.execute(
        "SELECT * FROM migration_reports WHERE id = ?", (result["report_id"],)
    ).fetchone()
assert report["status"] == "completed" and report["kind"] == "legacy_workspace_json"
assert json.loads(report["summary_json"])["messages"] == expected["messages"]
print("report", result["report_id"][:8], "status", report["status"])

banner("a conversation with children is refused outright, and nothing is written")
broken_home = tempfile.mkdtemp(prefix="broken-workspace-")
broken = store.Workspace(broken_home)
node = broken.projects[0]["children"][0]
node["children"] = [store.new_conversation("不該存在的子節點")]
broken.save()
before = len(services.tree.tree(admin)["projects"])
expect_error(409, import_workspace, services, admin, source_path=broken.path)
assert len(services.tree.tree(admin)["projects"]) == before
print("structurally invalid file rejected without a partial import")

banner("a failure part way through rolls the whole import back")
failing_home = tempfile.mkdtemp(prefix="failing-workspace-")
failing = store.Workspace(failing_home)
failing.rename(failing.projects[0]["id"], "會失敗的匯入")
victim = failing.add_conversation(failing.projects[0]["id"], "半路失敗")
for index in range(6):
    failing.append_message(victim["id"], "user", f"訊息 {index}")
failing.save()

# Break the import once it is several rows in, so there is real work to undo.
from tree_agent.server.migrations import legacy_workspace_import as importer  # noqa: E402

original_insert = importer.messages_repo.insert
attempts = {"count": 0}


def explode_part_way(*args, **kwargs):
    attempts["count"] += 1
    if attempts["count"] > 3:
        raise RuntimeError("simulated failure part way through the import")
    return original_insert(*args, **kwargs)


before = len(services.tree.tree(admin)["projects"])
importer.messages_repo.insert = explode_part_way
try:
    import_workspace(services, admin, source_path=failing.path)
except RuntimeError as exc:
    print("import failed after", attempts["count"], "messages:", exc)
else:
    raise AssertionError("expected the injected failure to abort the import")
finally:
    importer.messages_repo.insert = original_insert

after = services.tree.tree(admin)["projects"]
assert len(after) == before
assert all(p["name"] != "會失敗的匯入" for p in after)
with services.db.read() as conn:
    orphans = conn.execute(
        "SELECT count(*) FROM messages WHERE content LIKE '訊息 %'"
    ).fetchone()[0]
    failed = conn.execute(
        "SELECT count(*) FROM migration_reports WHERE status = 'failed'"
    ).fetchone()[0]
assert orphans == 0, orphans
assert failed == 1
print("nothing partial survived, and the failure was recorded in migration_reports")

banner("an over-long name is trimmed to fit rather than failing the whole import")
long_home = tempfile.mkdtemp(prefix="long-name-workspace-")
long_names = store.Workspace(long_home)
long_names.rename(long_names.projects[0]["id"], "長" * 400)
long_names.save()
trimmed = import_workspace(services, admin, source_path=long_names.path)
assert trimmed["status"] == "completed"
imported_names = [p["name"] for p in services.tree.tree(admin)["projects"]]
assert any(len(name) == 200 and set(name) == {"長"} for name in imported_names), imported_names
print("a 400-character project name became a 200-character one")

services.close()
print("\ntest_server_migration OK")
