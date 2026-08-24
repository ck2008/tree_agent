"""Branching a conversation with `codex exec fork`.

Measured semantics (codex 0.149) that this feature relies on:

    source thread ...02743  ->  knows PINEAPPLE-42
    fork of it    ...02744  ->  knows PINEAPPLE-42 + whatever was added after
    the source is NOT contaminated by anything added to the fork

So a fork is a copy-on-write branch: it inherits the history, gets a brand new
thread id from `thread.started`, and diverges from there.
"""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import tempfile, tkinter as tk
from tree_agent import store
from tree_agent import codex_runner as cr
from tree_agent.app import TreeAgentApp

# ---- the CLI invocation ----
cmd = cr.Turn("p", ".", lambda e: None, fork_from="SRC").build_command()
assert cmd[-3:] == ["fork", "SRC", "-"], cmd
assert "resume" not in cmd
# an existing thread always wins: a fork only happens on the very first turn
cmd = cr.Turn("p", ".", lambda e: None, thread_id="OWN", fork_from="SRC").build_command()
assert cmd[-3:] == ["resume", "OWN", "-"], cmd
assert "fork" not in cmd, "once it has its own thread it must resume, not re-fork"
# shared options still precede the sub-command
cmd = cr.Turn("p", ".", lambda e: None, fork_from="SRC", sandbox="read-only",
              model="gpt-5").build_command()
assert cmd.index("-s") < cmd.index("fork") and cmd.index("-m") < cmd.index("fork"), cmd
print("fork / resume command building OK")

# ---- the store operation ----
home = tempfile.mkdtemp()
ws = store.Workspace(home)
proj = ws.projects[0]
src = ws.add_conversation(proj["id"], "排查 401")
assert ws.fork_conversation(src["id"]) is None, "a conversation with no thread cannot fork"

ws.set_thread_id(src["id"], "01a02743-src")
ws.append_message(src["id"], "user", "為什麼會 401")
ws.append_message(src["id"], "agent", "redirect_uri 不一致")

fork = ws.fork_conversation(src["id"])
assert fork is not None
assert fork["name"] == "排查 401 (分岔)", fork["name"]
assert fork["fork_of"] == "01a02743-src"
assert fork["fork_of_name"] == "排查 401"
assert fork["thread_id"] is None, "the new thread only exists after the first send"
print("fork created:", fork["name"])

# it lands right after its source, in the same project
kids = [c["name"] for c in ws.find(proj["id"])["children"]]
assert kids.index("排查 401 (分岔)") == kids.index("排查 401") + 1, kids
assert ws.parent_of(fork["id"])["id"] == proj["id"]
print("placed next to its source in the same project OK")

# the shared history is visible, plus a marker for where they split
texts = [m["text"] for m in fork["messages"]]
assert "為什麼會 401" in texts and "redirect_uri 不一致" in texts
assert fork["messages"][-1]["role"] == "meta"
assert "分岔自「排查 401」" in fork["messages"][-1]["text"]
print("history copied with a split marker OK")

# the copy is independent — editing the fork must not touch the source
ws.append_message(fork["id"], "user", "只在分岔這邊")
assert "只在分岔這邊" not in [m["text"] for m in ws.find(src["id"])["messages"]]
assert len(ws.find(src["id"])["messages"]) == 2
print("source transcript untouched by the fork OK")

# ---- names do not stack suffixes, and never collide ----
second = ws.fork_conversation(src["id"])
assert second["name"] == "排查 401 (分岔 2)", second["name"]
ws.set_thread_id(fork["id"], "01a02744-fork")
third = ws.fork_conversation(fork["id"])          # fork of a fork
assert third["name"] == "排查 401 (分岔 3)", third["name"]
assert third["fork_of"] == "01a02744-fork", "must branch from the fork's own thread"
assert third["fork_of_name"] == "排查 401 (分岔)"

# repeated forks read in the order they were made, each under its own source
order = [c["name"] for c in ws.find(proj["id"])["children"]]
print("fork naming:", order)
assert order == [
    "新對話",
    "排查 401",
    "排查 401 (分岔)",      # first fork of 排查 401
    "排查 401 (分岔 3)",    # fork of the fork, sits under it
    "排查 401 (分岔 2)",    # second fork of 排查 401
], order

# ---- survives a reload ----
ws.save()
reloaded = store.Workspace(home).find(fork["id"])
assert reloaded["fork_of"] == "01a02743-src"
assert reloaded["fork_of_name"] == "排查 401"
print("fork metadata persisted OK")

# ---- the UI ----
home2 = tempfile.mkdtemp()
root = tk.Tk()
app = TreeAgentApp(root, home=home2, single_instance=False)
top = app.ws.projects[0]
conv = top["children"][0]
app.refresh_tree()
app._select(conv["id"])
root.update()

# no thread yet -> the button is disabled and forking is refused
assert app.conv_view.fork_button.instate(["disabled"])
import tkinter.messagebox as mb
shown = []
mb.showinfo = lambda title, msg, **k: shown.append(msg)
app.fork_conversation()
assert app.ws.find(conv["id"]) is not None
assert len(top["children"]) == 1, "nothing should have been created"
assert shown and "還沒送出過" in shown[0], shown
print("forking a never-run conversation is refused OK")

# give it a thread, and the button comes alive
app.ws.set_thread_id(conv["id"], "01a02743-ui")
app.ws.append_message(conv["id"], "agent", "既有回覆")
app.conv_view.show(app.ws.find(conv["id"]))
root.update()
assert not app.conv_view.fork_button.instate(["disabled"])

app.fork_conversation()
root.update()
assert len(top["children"]) == 2, [c["name"] for c in top["children"]]
new = top["children"][1]
assert app.current_id == new["id"], "the fork should be selected"
assert app.conv_view.conv_id == new["id"]
assert "分岔自" in app.conv_view.meta_label.cget("text")
assert "既有回覆" in app.conv_view.text.get("1.0", "end")
assert "🌿" in app.tree.item(new["id"], "text"), app.tree.item(new["id"], "text")
print("UI fork selects the branch, shows provenance and the 🌿 marker OK")

# the first send goes through `fork`, not `resume`
built = {}
real_turn = cr.Turn
class Spy(real_turn):
    def start(self):
        built["cmd"] = self.build_command()
cr.Turn = Spy
try:
    app.send(new["id"], "接著問")
finally:
    cr.Turn = real_turn
assert "fork" in built["cmd"], built["cmd"]
assert built["cmd"][built["cmd"].index("fork") + 1] == "01a02743-ui", built["cmd"]
assert "fork" in app.status.cget("text"), app.status.cget("text")
print("first send uses fork with the source thread id OK")

# once its own thread arrives, the marker and the mode both switch over
app.turns.pop(new["id"], None)
app._handle_event(new["id"], {"kind": "thread", "thread_id": "01a02744-ui"})
root.update()
assert app.ws.find(new["id"])["thread_id"] == "01a02744-ui"
assert "🌿" not in app.tree.item(new["id"], "text")
built.clear()
cr.Turn = Spy
try:
    app.send(new["id"], "再問一次")
finally:
    cr.Turn = real_turn
assert "resume" in built["cmd"] and "fork" not in built["cmd"], built["cmd"]
print("after the fork lands it resumes its own thread OK")

# the spy never emits "done", so drop the stub turn before closing —
# otherwise on_close() raises a real modal dialog and the test hangs.
app.turns.clear()
app.on_close()
print("\nALL FORK TESTS PASSED")
