"""Regression tests for the two bugs the earlier run exposed:
  1. a save() PermissionError must be retried, not thrown at the first attempt;
  2. an exception while handling one event must not kill the event pump.
"""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import tempfile, time, tkinter as tk
from tree_agent import store
from tree_agent.app import TreeAgentApp

# ---- 1. save() retries a transient sharing violation ----
home = tempfile.mkdtemp()
ws = store.Workspace(home)
real_replace = os.replace
calls = {"n": 0}
def flaky(src, dst):
    calls["n"] += 1
    if calls["n"] <= 4:
        raise PermissionError(5, "存取被拒。")
    return real_replace(src, dst)
os.replace = flaky
try:
    ws.save()
finally:
    os.replace = real_replace
assert calls["n"] == 5, calls
assert os.path.exists(ws.path)
print("save() retried a transient PermissionError OK (attempts:", calls["n"], ")")

# When the rename keeps being refused, the content is written in place rather
# than dropped. Measured on Windows: os.replace into %TEMP% under load fails
# past any short retry window, and the old code raised and lost the save.
ws.add_project(None, "必須留下的專案")
os.replace = lambda s, d: (_ for _ in ()).throw(PermissionError(5, "存取被拒。"))
try:
    before = ws.degraded_saves
    ws.save()                       # must not raise
    assert ws.degraded_saves == before + 1, ws.degraded_saves
finally:
    os.replace = real_replace
assert not ws._dirty, "a degraded save still clears the dirty flag"
names = [p["name"] for p in store.Workspace(home).projects]
assert "必須留下的專案" in names, names
print("persistent rename refusal falls back to an in-place write OK")

# a torn main file is recovered from the temp copy the fallback leaves behind
import shutil
home_torn = tempfile.mkdtemp()
ws_torn = store.Workspace(home_torn)
ws_torn.add_project(None, "要復原的專案")
ws_torn.save()
shutil.copy(ws_torn.path, ws_torn.path + ".tmp")
with open(ws_torn.path, "w", encoding="utf-8") as fh:
    fh.write('{"projects": [ TORN')
recovered = store.Workspace(home_torn)
assert "要復原的專案" in [p["name"] for p in recovered.projects]
assert os.path.exists(ws_torn.path + ".corrupt"), "the torn file is kept for inspection"
print("torn workspace recovered from the temp copy OK")

# a non-dict payload is treated as corrupt rather than crashing later
home_bad = tempfile.mkdtemp()
ws_bad = store.Workspace(home_bad)
with open(ws_bad.path, "w", encoding="utf-8") as fh:
    fh.write('["not", "a", "workspace"]')
assert store.Workspace(home_bad).projects, "must reseed instead of loading a list"
print("non-dict payload rejected OK")

# ---- 2. the pump survives a raising event handler ----
home2 = tempfile.mkdtemp()
root = tk.Tk()
app = TreeAgentApp(root, home=home2)
conv = app.ws.projects[0]["children"][0]
app.refresh_tree(conv["id"]); root.update()

boom = {"raised": False}
original = app._handle_event
def sabotage(conv_id, event):
    if not boom["raised"]:
        boom["raised"] = True
        raise RuntimeError("simulated handler failure")
    return original(conv_id, event)
app._handle_event = sabotage

# events are stamped with the conversation's current turn serial, so give the
# injected ones a serial the pump will accept
app.turn_serials[conv["id"]] = 1
app.events.put((conv["id"], 1, {"kind": "log", "text": "first — this one blows up"}))
app.events.put((conv["id"], 1, {"kind": "item", "role": "agent", "text": "SURVIVED"}))
for _ in range(30):
    root.update(); time.sleep(0.02)

assert boom["raised"]
texts = [m["text"] for m in app.ws.find(conv["id"])["messages"]]
assert "SURVIVED" in texts, texts
assert app._pump_job is not None, "pump stopped rescheduling"
print("pump survived a raising handler; status =", app.status.cget("text"))

# ---- 3. streamed appends are coalesced but still land on disk ----
app._handle_event = original
before = os.path.getmtime(app.ws.path)
for i in range(40):
    app.ws.append_message(conv["id"], "agent", f"chunk {i}")
assert app.ws._dirty, "appends should defer the write"
for _ in range(30):
    root.update(); time.sleep(0.02)
assert not app.ws._dirty, "pump should have flushed"
reloaded = store.Workspace(home2)
assert len(reloaded.find(conv["id"])["messages"]) == len(app.ws.find(conv["id"])["messages"])
print("coalesced writes flushed to disk OK")
app.on_close()
print("\nALL RESILIENCE TESTS PASSED")
