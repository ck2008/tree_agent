"""Tree rules: nesting, inheritance, naming, moves, forks, soft delete, revisions.

These are the behaviours `test_core.py` pins down for the JSON workspace. The
service has to keep every one of them while adding permissions and concurrency
on top, so this suite is deliberately a mirror of that one.
"""

from __future__ import annotations

from server_support import banner, expect_error, make_admin, make_services

services = make_services()
admin = make_admin(services)
tree = services.tree
messages = services.messages

banner("nesting and paths")
root = tree.create_project(admin, parent_id=None, name="我的專案")
sub = tree.create_project(admin, parent_id=root["id"], name="子專案A")
subsub = tree.create_project(admin, parent_id=sub["id"], name="子子專案A1")
conv = tree.create_conversation(admin, project_id=subsub["id"], name="對話X")
path = tree.path_of(admin, conv["id"])
print("path:", path)
assert path == "我的專案 / 子專案A / 子子專案A1 / 對話X", path

banner("settings inherit from the nearest ancestor that defines them")
tree.update_project(admin, root["id"], revision=root["revision"], fields={"cwd": r"E:\GitHub"})
assert tree.resolve(admin, conv["id"])["cwd"] == r"E:\GitHub"
sub = tree.get_project(admin, sub["id"])
tree.update_project(
    admin, sub["id"], revision=sub["revision"], fields={"cwd": r"E:\GitHub\ck2008"}
)
assert tree.resolve(admin, conv["id"])["cwd"] == r"E:\GitHub\ck2008"
# The shipped default is a deliberate choice; reference it, do not retype it.
from tree_agent.server.app import WORKSPACE_DEFAULTS  # noqa: E402

assert tree.resolve(admin, conv["id"])["sandbox"] == WORKSPACE_DEFAULTS["sandbox"]
subsub = tree.get_project(admin, subsub["id"])
tree.update_project(
    admin, subsub["id"], revision=subsub["revision"], fields={"sandbox": "read-only"}
)
assert tree.resolve(admin, conv["id"])["sandbox"] == "read-only"
print("resolved:", tree.resolve(admin, conv["id"]))

banner("a conversation's own model wins over what it would inherit")
conv_row = tree.get_conversation(admin, conv["id"])
tree.update_conversation(admin, conv["id"], revision=conv_row["revision"], fields={"model": "o3"})
assert tree.resolve(admin, conv["id"])["model"] == "o3"

banner("prompts accumulate down the tree instead of overriding")
for node, text in ((root, "全域規則"), (sub, "子專案規則"), (subsub, "最內層規則")):
    current = tree.get_project(admin, node["id"])
    tree.update_project(
        admin, node["id"], revision=current["revision"], fields={"prompt": text}
    )
combined = tree.instructions(admin, conv["id"])
print("instructions:", combined.replace("\n\n", " | "))
assert combined == "全域規則\n\n子專案規則\n\n最內層規則", combined

banner("sibling names are unique, and a clash is a 409")
expect_error(409, tree.create_project, admin, parent_id=root["id"], name="子專案A")
expect_error(409, tree.create_conversation, admin, project_id=subsub["id"], name="對話X")
# The generated placeholder is numbered instead of rejected.
first = tree.create_conversation(admin, project_id=root["id"])
second = tree.create_conversation(admin, project_id=root["id"])
print("auto names:", first["name"], "/", second["name"])
assert (first["name"], second["name"]) == ("新對話", "新對話 2")

banner("move legality")
expect_error(400, tree.move_project, admin, root["id"], revision=tree.get_project(admin, root["id"])["revision"], parent_id=subsub["id"])
expect_error(400, tree.move_project, admin, root["id"], revision=tree.get_project(admin, root["id"])["revision"], parent_id=root["id"])
moved = tree.move_conversation(
    admin, conv["id"], revision=tree.get_conversation(admin, conv["id"])["revision"], project_id=root["id"]
)
assert moved["project_id"] == root["id"]
print("moved conversation to:", tree.path_of(admin, conv["id"]))
current = tree.get_project(admin, sub["id"])
tree.move_project(admin, sub["id"], revision=current["revision"], parent_id=None)
assert tree.get_project(admin, sub["id"])["parent_id"] is None

banner("reordering within a level")
holder = tree.create_project(admin, parent_id=None, name="排序測試")
names = ["A", "B", "C"]
made = [tree.create_conversation(admin, project_id=holder["id"], name=n) for n in names]
order = lambda: [c["name"] for c in tree.list_conversations(admin, holder["id"])]
assert order() == names, order()
tree.move_conversation(
    admin,
    made[2]["id"],
    revision=tree.get_conversation(admin, made[2]["id"])["revision"],
    project_id=holder["id"],
    index=0,
)
print("after moving C to the front:", order())
assert order() == ["C", "A", "B"], order()

banner("two people editing the same node: the loser gets 409, not a silent overwrite")
target = tree.create_project(admin, parent_id=None, name="併發測試")
stale_revision = target["revision"]
tree.update_project(admin, target["id"], revision=stale_revision, fields={"name": "先寫入的"})
error = expect_error(
    409, tree.update_project, admin, target["id"], revision=stale_revision, fields={"name": "後寫入的"}
)
print("conflict reported current revision:", error.extra.get("current_revision"))
assert error.extra["current_revision"] == stale_revision + 1
assert tree.get_project(admin, target["id"])["name"] == "先寫入的"

banner("fork copies the transcript and records where it came from")
source = tree.create_conversation(admin, project_id=holder["id"], name="來源")
tree.set_runner_state(admin, source["id"], codex_thread_id="thread-abc")
for text in ("第一題", "第一個回答"):
    messages.append(admin, source["id"], role="user" if text == "第一題" else "agent", content=text)
forked = tree.fork_conversation(admin, source["id"])
print("fork:", forked["name"], "copied", forked["copied_messages"], "messages")
assert forked["name"] == "來源 (分岔)"
assert forked["forked_from_conversation_id"] == source["id"]
assert forked["forked_from_external_session_id"] == "thread-abc"
transcript = messages.list_messages(admin, forked["id"])["messages"]
assert [m["content"] for m in transcript[:2]] == ["第一題", "第一個回答"]
assert transcript[-1]["role"] == "meta" and "分岔自" in transcript[-1]["content"]
# A re-fork must not stack suffixes.
again = tree.fork_conversation(admin, source["id"])
print("second fork:", again["name"])
assert again["name"] == "來源 (分岔 2)"
# Repeated forks sit directly below their source, in the order they were made.
order = [c["name"] for c in tree.list_conversations(admin, holder["id"])]
print("sibling order:", order)
assert order[-3:] == ["來源", "來源 (分岔)", "來源 (分岔 2)"], order

banner("soft delete hides the whole subtree; restore brings it back")
doomed = tree.create_project(admin, parent_id=None, name="待刪除")
inner = tree.create_project(admin, parent_id=doomed["id"], name="內層")
inner_conv = tree.create_conversation(admin, project_id=inner["id"], name="內層對話")
messages.append(admin, inner_conv["id"], role="user", content="刪除前的訊息")
result = tree.delete_project(admin, doomed["id"], revision=tree.get_project(admin, doomed["id"])["revision"])
print("deleted:", result)
assert result["projects"] == 2 and result["conversations"] == 1 and result["messages"] == 1
assert all(p["name"] != "待刪除" for p in tree.tree(admin)["projects"])
expect_error(404, tree.get_project, admin, doomed["id"])
restored = tree.restore_project(admin, doomed["id"])
print("restored:", restored)
assert tree.get_project(admin, doomed["id"])["name"] == "待刪除"
assert messages.list_messages(admin, inner_conv["id"])["messages"][0]["content"] == "刪除前的訊息"

banner("restore refuses to collide with a name that appeared meanwhile")
tree.delete_project(admin, doomed["id"], revision=tree.get_project(admin, doomed["id"])["revision"])
tree.create_project(admin, parent_id=None, name="待刪除")
expect_error(409, tree.restore_project, admin, doomed["id"])
print("restore correctly refused a name clash")

banner("a nested delete does not resurrect what was already deleted")
outer = tree.create_project(admin, parent_id=None, name="兩段刪除")
early = tree.create_project(admin, parent_id=outer["id"], name="先刪的")
late = tree.create_project(admin, parent_id=outer["id"], name="後刪的")
tree.delete_project(admin, early["id"], revision=tree.get_project(admin, early["id"])["revision"])
tree.delete_project(admin, outer["id"], revision=tree.get_project(admin, outer["id"])["revision"])
tree.restore_project(admin, outer["id"])
assert tree.get_project(admin, late["id"])["name"] == "後刪的"
expect_error(404, tree.get_project, admin, early["id"])
print("the separately deleted child stayed deleted")

banner("reset clears the transcript and every runner id")
tree.set_runner_state(admin, source["id"], claude_session_id="claude-123")
cleared = tree.reset_conversation(admin, source["id"])
after = tree.get_conversation(admin, source["id"])
print("reset:", cleared)
assert after["codex_thread_id"] is None and after["claude_session_id"] is None
assert messages.list_messages(admin, source["id"])["messages"] == []

banner("token usage aggregates over a subtree")
usage_conv = tree.create_conversation(admin, project_id=holder["id"], name="用量")
for _ in range(2):
    reply = messages.append(admin, usage_conv["id"], role="agent", content="回答")
    messages.complete(
        admin, reply["id"], usage={"input_tokens": 100, "output_tokens": 20, "cached_input_tokens": 5}
    )
totals = messages.usage_for(admin, usage_conv["id"])
print("conversation usage:", totals)
assert totals["turns"] == 2 and totals["input_tokens"] == 200 and totals["output_tokens"] == 40
project_totals = messages.usage_for(admin, holder["id"])
assert project_totals["input_tokens"] == 200, project_totals

banner("a huge level still orders correctly after a resequence")
wide = tree.create_project(admin, parent_id=None, name="寬層")
for i in range(60):
    tree.create_conversation(admin, project_id=wide["id"], name=f"對話{i:02d}")
# Repeatedly insert at the same position to force the sort keys to run out.
for i in range(120):
    tree.create_conversation(admin, project_id=wide["id"], name=f"插入{i:03d}", index=1)
order = [c["name"] for c in tree.list_conversations(admin, wide["id"])]
assert order[0] == "對話00" and order[1] == "插入119", order[:3]
assert len(order) == 180 and len(set(order)) == 180
print("180 siblings ordered correctly; first three:", order[:3])

services.close()
print("\ntest_server_core OK")
