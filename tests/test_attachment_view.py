"""Attachments in the transcript: inline preview, clickable, opens the file."""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import tempfile, tkinter as tk
from tree_agent import clipboard_image as ci
from tree_agent.app import TreeAgentApp, THUMBNAIL_HEIGHT

tmp = tempfile.mkdtemp()


def write_png(name, w, h):
    path = os.path.join(tmp, name)
    with open(path, "wb") as fh:
        fh.write(ci._png(w, h, [bytes((10, 120, 200)) * w for _ in range(h)]))
    return path


shot = write_png("paste-1787371397051.png", 300, 150)
small = write_png("second.png", 20, 20)
undecodable = os.path.join(tmp, "photo.jpg")
open(undecodable, "wb").write(b"not a jpeg Tk can read")

home = tempfile.mkdtemp()
root = tk.Tk()
app = TreeAgentApp(root, home=home, single_instance=False)
conv = app.ws.projects[0]["children"][0]
app.refresh_tree(); app._select(conv["id"]); root.update()
view = app.conv_view

# ---- sending records the paths as metadata, not as text ----
app.send = TreeAgentApp.send.__get__(app)          # use the real implementation
from tree_agent import codex_runner as cr
real_turn = cr.Turn


class FakeTurn:
    def __init__(self, *a, **k): pass
    def start(self): pass
    def cancel(self): pass


cr.Turn = FakeTurn
try:
    app.send(conv["id"], "請看附加的圖片。", images=[shot, small])
finally:
    cr.Turn = real_turn
app._retire_turn(conv["id"])

message = app.ws.find(conv["id"])["messages"][-1]
assert message["text"] == "請看附加的圖片。", repr(message["text"])
assert "🖼" not in message["text"], "file names belong in metadata, not the text"
assert message["images"] == [shot, small], message["images"]
print("attachment paths stored as metadata OK")

# ---- the transcript shows a preview per image, each clickable ----
view.show(app.ws.find(conv["id"]))
root.update()
assert len(view._inline_images) == 2, len(view._inline_images)
for thumb in view._inline_images:
    assert thumb.height() <= THUMBNAIL_HEIGHT, thumb.height()
assert len(view._link_targets) == 2, view._link_targets
assert sorted(view._link_targets.values()) == sorted([shot, small])

body = view.text.get("1.0", "end")
assert "paste-1787371397051.png" in body and "second.png" in body
# the embedded images really are in the widget, not just the names
embedded = view.text.dump("1.0", "end", image=True)
assert len(embedded) == 2, embedded
print("two clickable previews embedded in the transcript OK")

# the file name carries the link tag, and so does the image index
tag = next(t for t, p in view._link_targets.items() if p == shot)
idx = view.text.search("paste-1787371397051.png", "1.0")
assert tag in view.text.tag_names(idx), view.text.tag_names(idx)
image_index = embedded[0][2]
assert tag in view.text.tag_names(image_index), view.text.tag_names(image_index)
assert str(view.text.tag_cget(tag, "underline")) in ("1", "True"), view.text.tag_cget(tag, "underline")
print("link tag applied to both the preview and the name OK")

# ---- clicking opens the file ----
opened = []
import tree_agent.app as app_mod
real_startfile = getattr(os, "startfile", None)
os.startfile = lambda p: opened.append(p)
try:
    view._open_link(tag)
finally:
    if real_startfile is not None:
        os.startfile = real_startfile
assert opened == [shot], opened
assert "已開啟" in app.status.cget("text"), app.status.cget("text")
print("clicking a preview opens the file OK")

# a deleted file reports instead of raising
missing_tag = "imgopen-missing"
view._link_targets[missing_tag] = os.path.join(tmp, "gone.png")
view._open_link(missing_tag)
assert "已不存在" in app.status.cget("text"), app.status.cget("text")
print("missing file reported, not raised OK")

# ---- undecodable images still get a clickable name ----
app.ws.append_message(conv["id"], "user", "這張呢", images=[undecodable])
view.show(app.ws.find(conv["id"]))
root.update()
assert len(view._link_targets) == 3, view._link_targets
assert len(view._inline_images) == 2, "no preview for what Tk cannot decode"
assert "photo.jpg" in view.text.get("1.0", "end")
print("undecodable image keeps a clickable name OK")

# ---- a legacy transcript that baked the names into the text shows them once ----
legacy = app.ws.add_conversation(app.ws.projects[0]["id"], "舊格式")
app.ws.append_message(legacy["id"], "user",
                      "請看附加的圖片。\n🖼 " + os.path.basename(shot), images=[shot])
app.refresh_tree(); app._select(legacy["id"]); root.update()
body = view.text.get("1.0", "end")
assert body.count(os.path.basename(shot)) == 1, body
assert "🖼" not in body, body
print("legacy baked-in name is not duplicated OK")

# ---- switching conversations releases the previous previews ----
app._select(conv["id"]); root.update()
assert len(view._inline_images) == 2
app._select(legacy["id"]); root.update()
assert len(view._inline_images) == 1, len(view._inline_images)
print("previews rebuilt per conversation OK")

app.on_close()
print("\nALL ATTACHMENT VIEW TESTS PASSED")
