"""Attachment previews and names open on left-click, with path copy on right-click."""

import os
import sys
import tempfile
import tkinter as tk
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tree_agent.app import THUMBNAIL_HEIGHT, TreeAgentApp

root = tk.Tk()
app = TreeAgentApp(root, home=tempfile.mkdtemp(), single_instance=False)
conversation = app.ws.projects[0]["children"][0]
app._select(conversation["id"])
view = app.conv_view

path = os.path.join(tempfile.mkdtemp(), "attachment.txt")
open(path, "w", encoding="utf-8").close()
view._write_attachment(path, "agent")
tag = "imgopen0"

opened = []
view._open_path = opened.append
view._open_link(tag)
assert opened == [path]
assert view.text.tk.call(view.text._w, "tag", "bind", tag, "<Button-1>")
assert view.text.tk.call(view.text._w, "tag", "bind", tag, "<Button-3>")
assert view._link_targets[tag] == path
assert THUMBNAIL_HEIGHT == 96

# The same save-as flow handles every attachment type because it copies bytes,
# not image data.  Keep the test on a plain text attachment to prove that.
destination = os.path.join(tempfile.mkdtemp(), "saved-attachment.txt")
with patch("tree_agent.app.filedialog.asksaveasfilename", return_value=destination):
    view._save_path_as(path)
assert open(destination, encoding="utf-8").read() == ""
assert "已另存為" in app.status.cget("text")

# A previewable image uses the enlarged thumbnail as its only visible label.
image_path = os.path.join(tempfile.mkdtemp(), "preview.gif")
image = tk.PhotoImage(width=4, height=4)
image.write(image_path, format="gif")
view._write_attachment(image_path, "agent")
assert os.path.basename(image_path) not in view.text.get("1.0", "end")

app.on_close()
print("attachments open directly, retain a right-click path action, and save as OK")
