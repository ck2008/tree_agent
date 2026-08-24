"""Unsent text must follow its own conversation, and must never be dropped."""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import tempfile, tkinter as tk
from tree_agent.app import TreeAgentApp

home = tempfile.mkdtemp()
root = tk.Tk()
app = TreeAgentApp(root, home=home)
top = app.ws.projects[0]
a = top["children"][0]
b = app.ws.add_conversation(top["id"], "B")
sub = app.ws.add_project(top["id"], "子專案")
c = app.ws.add_conversation(sub["id"], "C")
app.refresh_tree()
root.update()

view = app.conv_view

def draft_now():
    return view.input.get("1.0", "end").strip()

# type into A, switch to B -> B must be empty
app._select(a["id"]); root.update()
view.input.insert("1.0", "草稿給 A")
app._select(b["id"]); root.update()
assert draft_now() == "", repr(draft_now())
print("switching away does not leak the draft OK")

# type into B, hop to C (different project), then back
view.input.insert("1.0", "draft for B")
app._select(c["id"]); root.update()
assert draft_now() == ""
app._select(b["id"]); root.update()
assert draft_now() == "draft for B", repr(draft_now())
app._select(a["id"]); root.update()
assert draft_now() == "草稿給 A", repr(draft_now())
print("returning restores each conversation's own draft OK")

# a project in between must not disturb the drafts
app._select(sub["id"]); root.update()
app._select(a["id"]); root.update()
assert draft_now() == "草稿給 A"
print("selecting a project in between is harmless OK")

sent = []
app.send = lambda cid, prompt, images=None, review=None: sent.append((cid, prompt))
# sending while busy clears the composer and queues the turn
app.turns[a["id"]] = object()          # pretend a turn is in flight
view.on_send()
assert draft_now() == "", "queued text should leave the composer"
assert view.queued_count(a["id"]) == 1
del app.turns[a["id"]]
assert view.start_next_queued(a["id"])
assert sent == [(a["id"], "草稿給 A")], sent
print("busy conversation queues the text and starts it after completion OK")

view.input.insert("1.0", "再次送出")
view.on_send()
assert sent == [(a["id"], "草稿給 A"), (a["id"], "再次送出")], sent
assert draft_now() == ""
assert a["id"] not in view.drafts
app._select(b["id"]); root.update()
assert draft_now() == "draft for B", "sending A must not touch B's draft"
print("send clears only the sent draft OK")

app.on_close()
print("\nALL DRAFT TESTS PASSED")
