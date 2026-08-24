"""End-to-end: build the real GUI, drive a two-turn conversation through
codex exec + codex exec resume, and assert the transcript / thread state."""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import tempfile, time, tkinter as tk
from tree_agent import store
from tree_agent.app import TreeAgentApp

home = tempfile.mkdtemp()
workdir = os.path.join(home, "work")
os.makedirs(workdir, exist_ok=True)

root = tk.Tk()
app = TreeAgentApp(root, home=home)
root.update()
print("widgets built OK; status =", app.status.cget("text"))

# --- exercise tree building through the UI's own code paths ---
top = app.ws.projects[0]
app.ws.set_option(top["id"], "cwd", workdir)
app.ws.set_option(top["id"], "sandbox", "read-only")
sub = app.ws.add_project(top["id"], "子專案")
conv = app.ws.add_conversation(sub["id"], "煙霧測試")
app.refresh_tree(conv["id"])
root.update()
assert app.tree.exists(conv["id"])
assert app.tree.parent(conv["id"]) == sub["id"]
assert app.tree.parent(sub["id"]) == top["id"]
print("tree rows:", [app.tree.item(i, "text") for i in app.tree.get_children("")])

# selecting a project shows ProjectView; selecting a conversation shows ConversationView
app.tree.selection_set(sub["id"]); root.update()
assert app.proj_view.project_id == sub["id"]
assert app.proj_view.cwd_hint.cget("text").endswith(workdir), app.proj_view.cwd_hint.cget("text")
app.tree.selection_set(conv["id"]); root.update()
assert app.conv_view.conv_id == conv["id"]
print("header:", app.conv_view.meta_label.cget("text"))
assert workdir in app.conv_view.meta_label.cget("text")

def pump_until(pred, timeout, label):
    deadline = time.time() + timeout
    while time.time() < deadline:
        root.update()
        if pred():
            return True
        time.sleep(0.05)
    raise AssertionError(f"timeout waiting for {label}")

def transcript():
    return app.conv_view.text.get("1.0", "end")

# ---- turn 1: new thread ----
app.conv_view.input.insert("1.0", "Reply with exactly: PONG-ONE")
app.conv_view.on_send()
root.update()
assert app.conv_view.send_button.instate(["disabled"])
assert not app.conv_view.stop_button.instate(["disabled"])
assert conv["id"] in app.turns
assert "⏳" in app.tree.item(conv["id"], "text")
print("turn 1 running…")
pump_until(lambda: conv["id"] not in app.turns, 300, "turn 1")
t1 = transcript()
print("--- transcript after turn 1 ---")
print(t1.strip()[:900])
assert "PONG-ONE" in t1, t1
thread_id = app.ws.find(conv["id"])["thread_id"]
assert thread_id, "thread_id was not captured"
print("thread_id =", thread_id)
assert not app.conv_view.send_button.instate(["disabled"])
assert app.conv_view.stop_button.instate(["disabled"])
assert "⏳" not in app.tree.item(conv["id"], "text")

# ---- turn 2: resume must keep the same thread and remember turn 1 ----
app.conv_view.input.insert("1.0", "What exact word did I ask you to reply with a moment ago? Answer with just that word.")
app.conv_view.on_send()
print("turn 2 running (resume)…")
pump_until(lambda: conv["id"] not in app.turns, 300, "turn 2")
t2 = transcript()
print("--- transcript after turn 2 ---")
print(t2[len(t1):].strip()[:900])
assert app.ws.find(conv["id"])["thread_id"] == thread_id, "resume started a new thread"
assert "PONG-ONE" in t2[len(t1):], "context was not carried over by resume"
print("resume kept context OK")

# ---- persistence: reopen and confirm the transcript survives ----
saved_msgs = len(app.ws.find(conv["id"])["messages"])
app.current_id = conv["id"]
app.on_close()
try:
    root.update()
except tk.TclError:
    pass

root2 = tk.Tk()
app2 = TreeAgentApp(root2, home=home)
root2.update()
assert app2.current_id == conv["id"], (app2.current_id, conv["id"])
reloaded = app2.ws.find(conv["id"])
assert len(reloaded["messages"]) == saved_msgs
assert reloaded["thread_id"] == thread_id
assert "PONG-ONE" in app2.conv_view.text.get("1.0", "end")
print("reload restored selection + transcript OK")
# ---- a real fork: inherits the context, gets its own thread ----
app2.fork_conversation()
root2.update()
forked = app2.ws.parent_of(conv["id"])["children"][1]
assert forked["fork_of"] == thread_id, forked
assert forked["thread_id"] is None
assert "PONG-ONE" in app2.conv_view.text.get("1.0", "end"), "history should be visible"

def pump2_until(pred, timeout, label):
    deadline = time.time() + timeout
    while time.time() < deadline:
        root2.update()
        if pred():
            return
        time.sleep(0.05)
    raise AssertionError(f"timeout waiting for {label}")

app2.conv_view.input.insert("1.0", "What word did I ask you to reply with? Answer with just that word.")
app2.conv_view.on_send()
print("forked turn running (codex exec fork)…")
pump2_until(lambda: forked["id"] not in app2.turns, 300, "forked turn")

fork_thread = app2.ws.find(forked["id"])["thread_id"]
print("source thread:", thread_id)
print("fork   thread:", fork_thread)
assert fork_thread, "the fork never reported a thread id"
assert fork_thread != thread_id, "a fork must get its own thread"
answered = [m["text"] for m in app2.ws.find(forked["id"])["messages"][-3:] if m["role"] == "agent"]
assert any("PONG-ONE" in t for t in answered), answered
print("fork inherited the context and runs on its own thread OK")

# and the source thread is untouched by the fork
assert app2.ws.find(conv["id"])["thread_id"] == thread_id
assert len(app2.ws.find(conv["id"])["messages"]) == saved_msgs
print("source conversation untouched OK")

app2.on_close()

print("\nALL GUI TESTS PASSED")
