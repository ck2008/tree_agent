"""Tool calls stay in the information rail, never in the central answer."""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import tempfile
import tkinter as tk

from tree_agent.app import TOOL_HIDDEN, TreeAgentApp


def widget_text(widget):
    parts = []
    try:
        parts.append(str(widget.cget("text")))
    except tk.TclError:
        pass
    for child in widget.winfo_children():
        parts.extend(widget_text(child))
    return parts


home = tempfile.mkdtemp()
root = tk.Tk()
app = TreeAgentApp(root, home=home, single_instance=False)
conv = app.ws.projects[0]["children"][0]
tool = '$ pwsh -Command "Get-Content secret-plan.txt"\nfirst output\nsecond output'
app.ws.append_message(conv["id"], "agent", "我會先確認需求。")
app.ws.append_message(conv["id"], "tool", tool)
app.ws.append_message(conv["id"], "agent", "確認完成，這是結果。")
app.refresh_tree(); app._select(conv["id"]); root.update()

transcript = app.conv_view.text.get("1.0", "end")
assert tool not in transcript and "pwsh" not in transcript
assert "我會先確認需求。" in transcript and "這是結果。" in transcript
assert app.tool_display == TOOL_HIDDEN
assert not app.conv_view._tool_blocks

rail = "\n".join(widget_text(app.conv_view.info_body))
assert "工具紀錄（1）" in rail, rail
logs = [child for child in app.conv_view.info_body.winfo_children() if isinstance(child, tk.Frame)]
detail = logs[-1].winfo_children()[0].get("1.0", "end") if logs else ""
assert logs and tool.splitlines()[0] in detail and "first output" not in detail
print("tools are hidden from the transcript and retained in the information rail OK")

app.on_close()
print("\nALL TOOL DISPLAY TESTS PASSED")
