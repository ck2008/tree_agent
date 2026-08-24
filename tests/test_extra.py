"""Edge cases: empty tool items, parallel conversations, stop, delete-while-running,
and clean shutdown with no stray `after` callbacks."""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import tempfile, time, tkinter as tk
from tree_agent import store, codex_runner as cr
from tree_agent.app import TreeAgentApp

# empty collections must yield None, not ("tool", None)
for empty in ({"type": "file_change", "changes": []}, {"type": "todo_list", "items": []},
              {"type": "web_search"}, {"type": "reasoning", "text": ""}):
    assert cr.describe_item(empty) is None, empty
print("empty tool items -> None OK")

home = tempfile.mkdtemp()
workdir = os.path.join(home, "work"); os.makedirs(workdir)
root = tk.Tk()
app = TreeAgentApp(root, home=home)
top = app.ws.projects[0]
app.ws.set_option(top["id"], "cwd", workdir)
app.ws.set_option(top["id"], "sandbox", "read-only")
a = app.ws.add_conversation(top["id"], "平行A")
b = app.ws.add_conversation(top["id"], "平行B")
app.refresh_tree()
root.update()

def pump_until(pred, timeout, label):
    deadline = time.time() + timeout
    while time.time() < deadline:
        root.update()
        if pred():
            return
        time.sleep(0.05)
    raise AssertionError(f"timeout: {label}")

# ---- two conversations running at once ----
app.send(a["id"], "Reply with exactly: ALPHA")
app.send(b["id"], "Reply with exactly: BETA")
root.update()
assert len(app.turns) == 2, app.turns
print("two turns in flight:", sorted(app.turns))
pump_until(lambda: not app.turns, 300, "both turns")
ma = [m["text"] for m in app.ws.find(a["id"])["messages"] if m["role"] == "agent"]
mb = [m["text"] for m in app.ws.find(b["id"])["messages"] if m["role"] == "agent"]
print("A agent msgs:", ma, "| B agent msgs:", mb)
assert any("ALPHA" in t for t in ma), ma
assert any("BETA" in t for t in mb), mb
assert not any("BETA" in t for t in ma), "conversations leaked into each other"
assert not any("ALPHA" in t for t in mb), "conversations leaked into each other"
assert app.ws.find(a["id"])["thread_id"] != app.ws.find(b["id"])["thread_id"]
print("parallel isolation OK — distinct threads, no cross-talk")

# ---- stop mid-turn ----
c = app.ws.add_conversation(top["id"], "停止測試")
app.refresh_tree(); root.update()
app.send(c["id"], "Count slowly from 1 to 500, one number per line, with brief commentary on each.")
pump_until(lambda: c["id"] in app.turns, 10, "turn to register")
time.sleep(3.0); root.update()
app.stop_turn(c["id"])
pump_until(lambda: c["id"] not in app.turns, 60, "cancel to land")
roles = [m["role"] for m in app.ws.find(c["id"])["messages"]]
print("after stop, roles:", roles)
assert roles[-1] == "meta" and "停止" in app.ws.find(c["id"])["messages"][-1]["text"]
assert app.status.cget("text") == "已停止", app.status.cget("text")
print("stop OK")

# ---- delete a conversation while it is running ----
d = app.ws.add_conversation(top["id"], "刪除測試")
app.refresh_tree(); root.update()
app.send(d["id"], "Count slowly from 1 to 500 with commentary.")
pump_until(lambda: d["id"] in app.turns, 10, "turn to register")
app._select(d["id"])
import tkinter.messagebox as mb_mod
mb_mod.askyesno = lambda *a, **k: True          # auto-confirm the prompt
app.delete_selected()
root.update()
assert app.ws.find(d["id"]) is None
# late events for the deleted node must not raise
pump_until(lambda: d["id"] not in app.turns, 60, "cancelled turn to reap")
root.update()
print("delete-while-running OK (no exception from late events)")

# ---- clean shutdown: no stray `after` callback ----
app.on_close()
errors = []
def report(*a): errors.append(a)
root.report_callback_exception = report
for _ in range(20):
    try:
        root.update()
    except tk.TclError:
        break
    time.sleep(0.02)
assert not errors, errors
print("clean shutdown OK")
print("\nALL EDGE-CASE TESTS PASSED")
