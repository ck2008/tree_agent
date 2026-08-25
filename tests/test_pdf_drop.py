"""Dropping PDFs on the composer attaches them without adding path text."""
import os
import sys
import tempfile
import tkinter as tk
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tree_agent.app import TreeAgentApp

tmp = tempfile.mkdtemp()
pdf_path = os.path.join(tmp, "report with spaces.pdf")
text_path = os.path.join(tmp, "notes.txt")
open(pdf_path, "wb").close()
open(text_path, "wb").close()

root = tk.Tk()
app = TreeAgentApp(root, home=tempfile.mkdtemp(), single_instance=False)
conversation = app.ws.projects[0]["children"][0]
app._select(conversation["id"])
root.update()
view = app.conv_view
payload = root.tk.call("list", pdf_path, text_path)
result = view._on_file_drop(SimpleNamespace(data=payload))

assert result == "break"
assert view.current_attachments() == [pdf_path]
assert not view.input.get("1.0", "end").strip()
app.on_close()
print("PDF drops attach files and do not paste paths into the input OK")
