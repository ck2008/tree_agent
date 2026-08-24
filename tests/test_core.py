import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import tempfile, json
from tree_agent import store, codex_runner as cr

home = tempfile.mkdtemp()
ws = store.Workspace(home)
assert len(ws.projects) == 1, ws.projects
root = ws.projects[0]
print("seeded:", root["name"], [c["name"] for c in root["children"]])

# nesting
ws.set_option(root["id"], "cwd", r"E:\GitHub")
sub = ws.add_project(root["id"], "子專案A")
subsub = ws.add_project(sub["id"], "子子專案A1")
conv = ws.add_conversation(subsub["id"], "對話X")
print("path:", ws.path_of(conv["id"]))
assert ws.path_of(conv["id"]) == "我的專案 / 子專案A / 子子專案A1 / 對話X"

# inheritance
assert ws.resolve(conv["id"])["cwd"] == r"E:\GitHub", ws.resolve(conv["id"])
ws.set_option(sub["id"], "cwd", r"E:\GitHub\ck2008")
assert ws.resolve(conv["id"])["cwd"] == r"E:\GitHub\ck2008"
# reference the constant, not a literal: the shipped default is a
# deliberate choice that has changed once already
assert ws.resolve(conv["id"])["sandbox"] == store.DEFAULT_SANDBOX
ws.set_option(subsub["id"], "sandbox", "read-only")
assert ws.resolve(conv["id"])["sandbox"] == "read-only"
assert ws.inherited(subsub["id"], "cwd") == r"E:\GitHub\ck2008"
print("inheritance OK")

# move legality
assert ws.move(root["id"], subsub["id"]) is False, "must refuse cycle"
assert ws.move(conv["id"], None) is False, "conversation cannot be top level"
assert ws.move(conv["id"], root["id"]) is True
assert ws.parent_of(conv["id"])["id"] == root["id"]
assert ws.move(sub["id"], None) is True
assert len(ws.projects) == 2
print("move OK; top-level:", [p["name"] for p in ws.projects])

# reorder within the same parent
p = ws.projects[0]
a = ws.add_conversation(p["id"], "A")
b = ws.add_conversation(p["id"], "B")
names_before = [c["name"] for c in p["children"]]
ws.move(b["id"], p["id"], names_before.index("A"))
print("reorder:", [c["name"] for c in p["children"]])
assert [c["name"] for c in p["children"]].index("B") < [c["name"] for c in p["children"]].index("A")

# messages + thread
ws.append_message(conv["id"], "user", "hi")
ws.set_thread_id(conv["id"], "abc-123")
ws.clear_thread(conv["id"])
assert ws.find(conv["id"])["thread_id"] is None
assert ws.find(conv["id"])["messages"] == []

# delete subtree
ws.delete(p["id"])
assert ws.find(conv["id"]) is None
print("delete OK")

# persistence round trip
ws.data["ui"]["geometry"] = "1000x700+10+10"
ws.save()
ws2 = store.Workspace(home)
assert ws2.data["ui"]["geometry"] == "1000x700+10+10"
assert [x["name"] for x in ws2.projects] == [x["name"] for x in ws.projects]
print("persistence OK")

# corrupt-file recovery
with open(ws.path, "w", encoding="utf-8") as fh:
    fh.write("{not json")
ws3 = store.Workspace(home)
assert os.path.exists(ws.path + ".corrupt")
assert ws3.projects
print("corrupt recovery OK")

# ---- runner ----
t = cr.Turn("prompt", r"E:\GitHub", lambda e: None, sandbox="workspace-write", model="gpt-5")
print("new:", t.describe_command())
assert t.build_command()[-1] == "-"
t2 = cr.Turn("prompt", r"E:\GitHub", lambda e: None, thread_id="0199-xyz", sandbox="read-only")
print("resume:", t2.describe_command())
cmd = t2.build_command()
assert cmd[-3:] == ["resume", "0199-xyz", "-"], cmd
assert cmd.index("-s") < cmd.index("resume"), "options must precede the subcommand"

# item formatting
cases = [
    {"type": "agent_message", "text": "hello"},
    {"type": "reasoning", "text": "thinking"},
    {"type": "error", "message": "boom"},
    {"type": "command_execution", "command": "echo hi", "exit_code": 0, "aggregated_output": "hi\n"},
    {"type": "command_execution", "command": "bad", "exit_code": 2, "aggregated_output": "err"},
    {"type": "file_change", "changes": [{"kind": "update", "path": "a.py"}]},
    {"type": "todo_list", "items": [{"text": "one", "completed": True}]},
    {"type": "web_search", "query": "codex"},
    {"type": "mcp_tool_call", "server": "s", "tool": "t", "status": "ok"},
    {"type": "brand_new_thing", "foo": 1},
    {"type": "agent_message", "text": "   "},
]
for c in cases:
    print(" ", c["type"], "->", cr.describe_item(c))
assert cr.describe_item(cases[-1]) is None
assert cr.describe_item(cases[3])[0] == "tool"
print("\nALL CORE TESTS PASSED")
