"""Regression: events from a retired turn must not resurrect a conversation.

Found by running the new review action against this repo:

    "Conversation reset can retain or restore old Codex context, contradicting
     its stated behavior of clearing the conversation and starting a new thread."

Cancelling a turn is asynchronous — the reader threads may already have queued
`thread` and `item` events. Reset wipes the conversation, then the pump replays
those events and puts the old thread id and messages straight back.
"""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import tempfile, time, tkinter as tk
import tkinter.messagebox as mb
from tree_agent.app import TreeAgentApp

# Patch BOTH: a stray modal dialog in a test driven by update() rather than
# mainloop() has nobody to dismiss it, and blocks the run indefinitely.
mb.askyesno = lambda *a, **k: True          # auto-confirm the reset / delete prompts
_dialogs = []
mb.showinfo = lambda title, msg, **k: _dialogs.append(msg)

home = tempfile.mkdtemp()
root = tk.Tk()
app = TreeAgentApp(root, home=home, single_instance=False)
top = app.ws.projects[0]
conv = top["children"][0]
app.refresh_tree()
app._select(conv["id"])
root.update()


def drain():
    for _ in range(20):
        root.update()
        time.sleep(0.02)


class FakeTurn:
    """Stands in for a live turn without launching codex."""
    def __init__(self, *a, **k):
        self.cancelled = False
    def start(self):
        pass
    def cancel(self):
        self.cancelled = True


from tree_agent import codex_runner as cr
real_turn = cr.Turn
cr.Turn = FakeTurn
try:
    app.send(conv["id"], "第一個問題")
finally:
    cr.Turn = real_turn

serial = app.turn_serials[conv["id"]]
assert conv["id"] in app.turns

# the turn reports a thread and some output — as a real one would
app.events.put((conv["id"], serial, {"kind": "thread", "thread_id": "01a-OLD"}))
app.events.put((conv["id"], serial, {"kind": "item", "role": "agent", "text": "舊的回覆"}))
drain()
assert app.ws.find(conv["id"])["thread_id"] == "01a-OLD"
assert "舊的回覆" in [m["text"] for m in app.ws.find(conv["id"])["messages"]]
print("live turn's events are applied normally OK")

# ---- now reset while events are still in flight ----
app.events.put((conv["id"], serial, {"kind": "item", "role": "agent", "text": "遲到的回覆"}))
app.events.put((conv["id"], serial, {"kind": "thread", "thread_id": "01a-OLD"}))
app.reset_conversation()                     # wipes thread + messages
drain()                                      # the stale events get pumped here

reloaded = app.ws.find(conv["id"])
assert reloaded["thread_id"] is None, f"reset was undone: {reloaded['thread_id']}"
assert reloaded["messages"] == [], reloaded["messages"]
assert conv["id"] not in app.turns
assert conv["id"] not in app.turn_serials
assert "遲到的回覆" not in app.conv_view.text.get("1.0", "end")
print("stale events after reset are dropped OK")

# a fresh turn afterwards gets a new serial and works
cr.Turn = FakeTurn
try:
    app.send(conv["id"], "重設後的新問題")
finally:
    cr.Turn = real_turn
new_serial = app.turn_serials[conv["id"]]
assert new_serial != serial, (serial, new_serial)
app.events.put((conv["id"], new_serial, {"kind": "thread", "thread_id": "01a-NEW"}))
drain()
assert app.ws.find(conv["id"])["thread_id"] == "01a-NEW"
print("a new turn after reset works and gets a fresh serial OK")

# ---- the same guard protects a deleted conversation ----
victim = app.ws.add_conversation(top["id"], "要被刪的")
app.refresh_tree()
app._select(victim["id"])
root.update()
cr.Turn = FakeTurn
try:
    app.send(victim["id"], "問題")
finally:
    cr.Turn = real_turn
victim_serial = app.turn_serials[victim["id"]]
app.events.put((victim["id"], victim_serial, {"kind": "item", "role": "agent", "text": "孤兒事件"}))
app.delete_selected()
drain()
assert app.ws.find(victim["id"]) is None
assert victim["id"] not in app.turns
assert victim["id"] not in app.turn_serials
print("stale events after delete are dropped OK")

# ---- stopping is different: its `done` must still be delivered ----
app._select(conv["id"])
root.update()
# the previous turn never reported `done`, so retire it or `send` would refuse
# the next one with a modal "still running" dialog
app._retire_turn(conv["id"])
cr.Turn = FakeTurn
try:
    app.send(conv["id"], "會被停止的問題")
finally:
    cr.Turn = real_turn
stop_serial = app.turn_serials[conv["id"]]
app.stop_turn(conv["id"])
assert conv["id"] in app.turns, "stop must not retire the turn — done still has to arrive"
app.events.put((conv["id"], stop_serial, {"kind": "done", "returncode": 1, "cancelled": True}))
drain()
assert conv["id"] not in app.turns
assert app.ws.find(conv["id"])["messages"][-1]["role"] == "meta"
assert "已停止" in app.status.cget("text"), app.status.cget("text")
print("stop still receives its done event OK")

assert not _dialogs, f"no test step should have raised a dialog: {_dialogs}"
app.turns.clear()
app.on_close()
print("\nALL STALE-EVENT TESTS PASSED")
