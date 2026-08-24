"""Command executions must not bury the answer they were working towards.

A single `git status` or file listing from Codex can be hundreds of lines. The
default is therefore one clickable summary line per command, with the output
inserted but hidden via the tag's `elide` option — expanding is instant and does
not disturb the scroll position.
"""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import tempfile, tkinter as tk
from tree_agent import store
from tree_agent.app import (TreeAgentApp, TOOL_COLLAPSED, TOOL_FULL, TOOL_HIDDEN,
                            TOOL_SUMMARY_CHARS)

LONG_COMMAND = ('$ "C:\\Program Files\\PowerShell\\7\\pwsh.exe" -Command '
                "'$files = rg --files -g AGENTS.md -g \"!node_modules\" -g \"!vendor\"; "
                "git status --short --branch'")
OUTPUT = "\n".join(["## master...origin/master"] + [f"?? ../dir_{i}/" for i in range(60)])
TOOL_TEXT = LONG_COMMAND + "\n" + OUTPUT
LINE_COUNT = OUTPUT.count("\n") + 1

home = tempfile.mkdtemp()
root = tk.Tk()
app = TreeAgentApp(root, home=home, single_instance=False)
conv = app.ws.projects[0]["children"][0]
app.ws.append_message(conv["id"], "agent", "我會依序檢查專案結構。")
app.ws.append_message(conv["id"], "tool", TOOL_TEXT)
app.ws.append_message(conv["id"], "agent", "掃描完成，結論如下。")
app.refresh_tree(); app._select(conv["id"]); root.update()
view = app.conv_view


def displayed_lines():
    return view.text.count("1.0", "end", "displaylines")[0]


# ---- collapsed is the default ----
assert app.tool_display == TOOL_COLLAPSED, app.tool_display
collapsed_lines = displayed_lines()
body = view.text.get("1.0", "end")
assert f"（{LINE_COUNT} 行輸出）" in body, "the summary states how much is hidden"
assert "?? ../dir_59/" in body, "the output is present in the widget, just hidden"
assert len(view._tool_blocks) == 1, view._tool_blocks
# the answer either side of the command stays close together
assert collapsed_lines < 15, f"collapsed transcript should be short, got {collapsed_lines}"
print(f"collapsed to {collapsed_lines} display lines (output of {LINE_COUNT} lines hidden) OK")

# the long command line is truncated on the summary, not wrapped forever
summary_line = next(l for l in body.split("\n") if "pwsh.exe" in l)
assert "…" in summary_line, summary_line
assert len(summary_line) < TOOL_SUMMARY_CHARS + 40, len(summary_line)
print("over-long command truncated on the summary line OK")

# ---- clicking expands, and clicking again collapses ----
view._toggle_tool(0)
root.update()
expanded_lines = displayed_lines()
assert expanded_lines > collapsed_lines + 50, (collapsed_lines, expanded_lines)
view._toggle_tool(0)
root.update()
assert displayed_lines() == collapsed_lines, (displayed_lines(), collapsed_lines)
print(f"toggle expands to {expanded_lines} lines and collapses back OK")

# the arrow flips with the state, and only one of the two is ever visible
closed, opened, body_id = view._tool_blocks[0]
def elided(tag):
    return str(view.text.tag_cget(tag, "elide")) in ("1", "true", "True")
assert not elided(closed) and elided(opened) and elided(body_id)
view._toggle_tool(0)
assert elided(closed) and not elided(opened) and not elided(body_id)
view._toggle_tool(0)
print("arrow glyph tracks the state OK")

# hidden output is still copyable, because it is really in the widget
view.select_all()
assert "?? ../dir_59/" in view.selection(), "Ctrl+A must still capture hidden output"
print("hidden output remains selectable and copyable OK")

# ---- full mode shows everything ----
app.tool_display_var.set(TOOL_FULL)
app.apply_tool_display()
root.update()
full_lines = displayed_lines()
assert full_lines > collapsed_lines + 50, (collapsed_lines, full_lines)
assert view._tool_blocks == [], "full mode needs no toggles"
assert "行輸出）" not in view.text.get("1.0", "end")
print(f"full mode draws all {full_lines} lines OK")

# ---- hidden mode drops the block from the view but not from the data ----
app.tool_display_var.set(TOOL_HIDDEN)
app.apply_tool_display()
root.update()
body = view.text.get("1.0", "end")
assert "?? ../dir_59/" not in body and "pwsh.exe" not in body, body[:200]
assert "我會依序檢查專案結構。" in body and "掃描完成" in body, "the prose must survive"
assert displayed_lines() < collapsed_lines + 2
roles = [m["role"] for m in app.ws.find(conv["id"])["messages"]]
assert roles.count("tool") == 1, "hiding must not delete the record"
print("hidden mode removes it from the view but keeps the record OK")

# ---- the choice survives a restart ----
app.tool_display_var.set(TOOL_COLLAPSED)
app.apply_tool_display()
app.on_close()
root2 = tk.Tk()
app2 = TreeAgentApp(root2, home=home, single_instance=False)
assert app2.tool_display == TOOL_COLLAPSED, app2.tool_display
app2.tool_display_var.set(TOOL_HIDDEN)
app2.apply_tool_display()
app2.on_close()
root3 = tk.Tk()
app3 = TreeAgentApp(root3, home=home, single_instance=False)
assert app3.tool_display == TOOL_HIDDEN, app3.tool_display
print("preference persisted across restarts OK")

# a bad stored value falls back rather than breaking the view. It has to be
# written after closing: on_close() rewrites the key with the live value.
app3.on_close()
import json
with open(os.path.join(home, "workspace.json"), encoding="utf-8") as fh:
    raw = json.load(fh)
raw.setdefault("ui", {})["tool_display"] = "nonsense"
with open(os.path.join(home, "workspace.json"), "w", encoding="utf-8") as fh:
    json.dump(raw, fh, ensure_ascii=False)
root4 = tk.Tk()
app4 = TreeAgentApp(root4, home=home, single_instance=False)
assert app4.tool_display == TOOL_COLLAPSED, app4.tool_display
print("unknown stored mode falls back to collapsed OK")

# a single-line tool message needs no toggle at all
single = app4.ws.add_conversation(app4.ws.projects[0]["id"], "單行")
app4.ws.append_message(single["id"], "tool", "$ echo hi")
app4.refresh_tree(); app4._select(single["id"]); root4.update()
assert app4.conv_view._tool_blocks == [], app4.conv_view._tool_blocks
assert "$ echo hi" in app4.conv_view.text.get("1.0", "end")
print("single-line command drawn as-is OK")

app4.on_close()
print("\nALL TOOL DISPLAY TESTS PASSED")
