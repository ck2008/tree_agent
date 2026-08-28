"""Pasted terminal output is shown in a dedicated scrollable log block."""
import os
import sys
import tempfile
import tkinter as tk
from tkinter import ttk

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tree_agent.app import TreeAgentApp, _is_terminal_log

terminal_output = """npm WARN EBADENGINE Unsupported engine
Error: listen EACCES: permission denied 127.0.0.1:3080
    at Server.setupListenHandle [as _listen2] (node:net:1918:21)
    at listenInCluster (node:net:1997:12)"""

assert _is_terminal_log(terminal_output)
assert not _is_terminal_log("第一行\n第二行\n第三行")

root = tk.Tk()
app = TreeAgentApp(root, home=tempfile.mkdtemp(), single_instance=False)
conversation = app.ws.projects[0]["children"][0]
app._select(conversation["id"])
view = app.conv_view
view.append("user", terminal_output)

assert len(view._inline_log_widgets) == 1
log = view._inline_log_widgets[0].winfo_children()[1]
assert log.cget("wrap") == "none"
assert log.get("1.0", "end-1c") == terminal_output
assert "你（貼上的日誌）" in view.text.get("1.0", "end")

view.append("agent", "執行以下指令：\n\n```powershell\nnode -v\nnpm -v\n```")
assert len(view._inline_code_widgets) == 1
code_frame = view._inline_code_widgets[0]
code_body = code_frame.winfo_children()[1]
copy_button = code_frame.winfo_children()[0].winfo_children()[1]
assert code_body.get("1.0", "end-1c") == "node -v\nnpm -v"
assert str(code_body.cget("state")) == "disabled"
assert code_body.bind("<Control-c>"), "code text must support copying a selection"
assert not any(isinstance(child, ttk.Scrollbar) for child in code_frame.winfo_children())
assert copy_button.cget("text") == "複製"
assert code_body.bind("<MouseWheel>"), "code card must forward mouse-wheel input"
view._copy_block("copied block")
assert root.clipboard_get() == "copied block"
app.on_close()
print("terminal output and fenced code use scrollable copyable blocks OK")
