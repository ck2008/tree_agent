"""Built-in agent selection, configuration persistence, and Claude event parsing."""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import json, tempfile, tkinter as tk
from tree_agent import codex_runner as cr
from tree_agent import store
from tree_agent.app import AGENT_LABELS, AgentsDialog, TreeAgentApp

home = tempfile.mkdtemp()
ws = store.Workspace(home)
conv = ws.projects[0]["children"][0]
assert ws.conversation_agent(conv["id"]) == store.CODEX_AGENT
ws.set_conversation_agent(conv["id"], store.CLAUDE_AGENT)
ws.set_agent_path(store.CLAUDE_AGENT, "")
ws2 = store.Workspace(home)
assert ws2.conversation_agent(conv["id"]) == store.CLAUDE_AGENT
assert ws2.agent_path(store.CLAUDE_AGENT) is None
print("conversation Agent defaults to Codex and persists its selection OK")

events = []
turn = cr.ClaudeTurn("hi", ".", events.append)
turn._handle_stdout_line(json.dumps({"type": "system", "subtype": "init", "session_id": "claude-1"}))
turn._handle_stdout_line(json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": "hello"}]}}))
assert events == [
    {"kind": "session", "session_id": "claude-1"},
    {"kind": "item", "role": "agent", "text": "hello"},
], events
print("Claude stream init and assistant text events map to the UI protocol OK")

root = tk.Tk()
app = TreeAgentApp(root, home=tempfile.mkdtemp(), single_instance=False)
node = app.ws.projects[0]["children"][0]
app.refresh_tree(); app._select(node["id"]); root.update()
view = app.conv_view
assert view.agent_var.get() == AGENT_LABELS[store.CODEX_AGENT]
view.agent_var.set(AGENT_LABELS[store.CLAUDE_AGENT])
view._on_agent_changed()
assert app.ws.conversation_agent(node["id"]) == store.CLAUDE_AGENT
app.handle_slash_command(node["id"], "/status")
messages = app.ws.find(node["id"])["messages"]
assert messages[-1]["role"] == "agent" and messages[-1]["agent_id"] == store.CLAUDE_AGENT
assert "Agent: Claude Code" in messages[-1]["text"]
assert "Claude Code" in view.text.get("1.0", "end")
app.handle_slash_command(node["id"], "/compact")
assert app.ws.find(node["id"])["messages"][-1]["role"] == "notice"
app._handle_event(node["id"], {"kind": "item", "role": "tool", "text": "工具：Read"})
assert app.ws.find(node["id"])["messages"][-1]["role"] == "agent_tool"
assert "工具：Read" not in view.text.get("1.0", "end")
tool_logs = [w for holder in view.info_body.winfo_children() for w in holder.winfo_children()
             if isinstance(w, tk.Text)]
assert tool_logs and "1. 工具：Read" in tool_logs[-1].get("1.0", "end")
dialog = AgentsDialog(root, app)
root.update()
assert set(dialog.vars) == {store.CODEX_AGENT, store.CLAUDE_AGENT}
dialog.destroy()
app.on_close()
print("conversation picker, /status, and scrollable Claude tool log work OK")

print("\nALL AGENT TESTS PASSED")
