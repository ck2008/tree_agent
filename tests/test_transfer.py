"""Export and import: a project, a conversation, or the whole workspace.

Attachments live outside workspace.json, so the portable unit is a zip holding
the manifest plus the referenced image files. Two things are reported rather
than papered over: node ids are regenerated (so importing twice gives two
copies, not a collision), and a `thread_id` only resolves on the machine whose
`~/.codex/sessions` created it.
"""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import json, socket, tempfile, zipfile
from tree_agent import clipboard_image as ci
from tree_agent import store, transfer

# ---------- a workspace worth exporting ----------
home = tempfile.mkdtemp()
ws = store.Workspace(home)
top = ws.projects[0]
ws.rename(top["id"], "客戶A")
# drop the conversation the seed created, so the counts below are unambiguous
for seeded in list(top["children"]):
    ws.delete(seeded["id"])
ws.set_option(top["id"], "cwd", tempfile.gettempdir())
sub = ws.add_project(top["id"], "認證中心")
conv = ws.add_conversation(sub["id"], "排查 401")
ws.set_thread_id(conv["id"], "01a-thread-abc")
ws.append_message(conv["id"], "user", "為什麼會 401")
ws.append_message(conv["id"], "agent", "redirect_uri 不一致")
ws.append_message(conv["id"], "tool", "$ git status\n?? a.txt")
ws.add_usage(conv["id"], {"input_tokens": 100, "output_tokens": 20})

shot = os.path.join(home, "shot.png")
with open(shot, "wb") as fh:
    fh.write(ci._png(8, 8, [bytes((1, 2, 3)) * 8 for _ in range(8)]))
gone = os.path.join(home, "deleted.png")
ws.append_message(conv["id"], "user", "看這張", images=[shot, gone])
ws.save()

# ---------- export ----------
archive = os.path.join(tempfile.mkdtemp(), "export.zip")
summary = transfer.export_nodes(ws, [top["id"]], archive)
print("export:", {k: v for k, v in summary.items() if k != "path"})
assert summary["projects"] == 2 and summary["conversations"] == 1, summary
assert summary["attachments"] == 1, "only the file that still exists"
assert summary["missing_attachments"] == [gone], summary["missing_attachments"]

with zipfile.ZipFile(archive) as zf:
    names = zf.namelist()
    assert transfer.MANIFEST in names, names
    assert any(n.startswith("attachments/") for n in names), names
    manifest = json.loads(zf.read(transfer.MANIFEST).decode("utf-8"))
assert manifest["format"] == transfer.FORMAT and manifest["version"] == transfer.VERSION
assert manifest["source_host"] == socket.gethostname()
# the archive references its own copies, not machine-specific paths
exported_images = manifest["nodes"][0]["children"][0]["children"][0]["messages"][-1]["images"]
assert exported_images and all(p.startswith("attachments/") for p in exported_images), exported_images
print("archive holds the manifest and its attachments OK")

# ---------- import into a different workspace ----------
other = store.Workspace(tempfile.mkdtemp())
target = other.projects[0]
before = len(target["children"])
result = transfer.import_archive(other, archive, target["id"])
print("import:", {k: v for k, v in result.items() if k != "roots"})
assert result["projects"] == 2 and result["conversations"] == 1, result
assert result["attachments"] == 1, result
assert result["threads"] == 1, result
assert len(target["children"]) == before + 1

imported = other.find(result["roots"][0])
assert imported["name"] == "客戶A"
# ids were regenerated
assert imported["id"] != top["id"]
imported_conv = next(c for c, _ in other.walk() if c["kind"] == store.CONVERSATION
                     and c["name"] == "排查 401")
assert imported_conv["id"] != conv["id"]
assert imported_conv["thread_id"] == "01a-thread-abc", "the thread reference travels"
assert imported_conv["usage"]["input_tokens"] == 100, "usage travels"
texts = [m["text"] for m in imported_conv["messages"]]
assert "redirect_uri 不一致" in texts and "$ git status\n?? a.txt" in texts
print("structure, messages, thread and usage all imported OK")

# attachments were extracted into the destination workspace and re-pointed
images = imported_conv["messages"][-1]["images"]
assert images and len(images) == 1, images
assert os.path.isfile(images[0]), images
assert os.path.dirname(images[0]) == os.path.join(other.home, "attachments"), images
assert open(images[0], "rb").read()[:8] == b"\x89PNG\r\n\x1a\n"
print("attachment extracted into the destination workspace OK")

# ---------- importing twice makes two copies, never a collision ----------
again = transfer.import_archive(other, archive, target["id"])
names = [c["name"] for c in target["children"]]
assert names.count("客戶A") == 1 and any(n.startswith("客戶A ") for n in names), names
assert again["roots"][0] != result["roots"][0]
ids = [n["id"] for n, _ in other.walk()]
assert len(ids) == len(set(ids)), "duplicate ids after a second import"
# and the second import did not overwrite the first attachment file
assert again["attachments"] == 1
files = os.listdir(os.path.join(other.home, "attachments"))
assert len(files) == 2, files
print("second import is a separate copy, attachments not overwritten OK:", names)

# ---------- top-level import of a bare conversation gets a home ----------
conv_only = os.path.join(tempfile.mkdtemp(), "conv.zip")
transfer.export_nodes(ws, [conv["id"]], conv_only)
fresh = store.Workspace(tempfile.mkdtemp())
res = transfer.import_archive(fresh, conv_only, None)
roots = [n["name"] for n in fresh.projects]
assert res["conversations"] == 1, res
# a conversation cannot sit at the top level, so it is wrapped in a project
assert all(n["kind"] == store.PROJECT for n in fresh.projects), roots
assert any(n["name"] == "匯入的對話" for n in fresh.projects), roots
print("a bare conversation imported at top level is wrapped in a project OK")

# ---------- whole-workspace round trip ----------
whole = os.path.join(tempfile.mkdtemp(), "all.zip")
w_summary = transfer.export_workspace(ws, whole)
assert w_summary["projects"] >= 2
empty = store.Workspace(tempfile.mkdtemp())
empty.data["projects"] = []
transfer.import_archive(empty, whole, None)
assert [n["name"] for n in empty.projects] == [n["name"] for n in ws.projects]
print("whole-workspace round trip OK")

# ---------- rejects what it should ----------
bad = os.path.join(tempfile.mkdtemp(), "bad.zip")
with zipfile.ZipFile(bad, "w") as zf:
    zf.writestr(transfer.MANIFEST, json.dumps({"format": "something-else"}))
for path, why in [(bad, "wrong format"),
                  (os.path.join(tempfile.mkdtemp(), "missing.zip"), "no such file")]:
    try:
        transfer.read_manifest(path)
        raise AssertionError(f"should have rejected: {why}")
    except transfer.TransferError:
        pass

future = os.path.join(tempfile.mkdtemp(), "future.zip")
with zipfile.ZipFile(future, "w") as zf:
    zf.writestr(transfer.MANIFEST, json.dumps(
        {"format": transfer.FORMAT, "version": transfer.VERSION + 5, "nodes": [1]}))
try:
    transfer.read_manifest(future)
    raise AssertionError("should have rejected a newer format version")
except transfer.TransferError as exc:
    assert str(transfer.VERSION) in str(exc), exc

# a bare manifest (no zip) imports, but says nothing about attachments
plain = os.path.join(tempfile.mkdtemp(), "manifest.json")
with open(plain, "w", encoding="utf-8") as fh:
    json.dump(manifest, fh, ensure_ascii=False)
bare_ws = store.Workspace(tempfile.mkdtemp())
bare = transfer.import_archive(bare_ws, plain, None)
assert bare["attachments"] == 0, bare
# by name: the destination workspace has a seeded conversation of its own
bare_conv = next(c for c, _ in bare_ws.walk()
                 if c["kind"] == store.CONVERSATION and c["name"] == "排查 401")
assert bare_conv["messages"][-1]["images"] is None, "dangling references dropped"
print("invalid, future and attachment-less archives all handled OK")

# ---------- markdown is one-way but readable ----------
md = os.path.join(tempfile.mkdtemp(), "out.md")
md_summary = transfer.export_markdown(ws, top["id"], md)
body = open(md, encoding="utf-8").read()
assert md_summary["conversations"] == 1
assert "# 客戶A" in body and "**Codex**" in body and "**你**" in body
assert "redirect_uri 不一致" in body
assert "```" in body and "$ git status" in body, "tool output is fenced"
assert "01a-thread-abc" in body
assert "![shot.png]" in body, "attachments referenced"
print("markdown export readable OK")

# a single conversation exports too
md2 = os.path.join(tempfile.mkdtemp(), "one.md")
transfer.export_markdown(ws, conv["id"], md2)
assert "排查 401" in open(md2, encoding="utf-8").read()

try:
    transfer.export_nodes(ws, ["no-such-id"], os.path.join(tempfile.mkdtemp(), "x.zip"))
    raise AssertionError("exporting nothing should fail")
except transfer.TransferError:
    pass
print("empty export rejected OK")

# ---------- the actions are reachable from the tree's context menu ----------
# They originally lived only in the File menu, which is not where you look when
# you want to do something to a particular node.
import tkinter as tk
import tkinter.filedialog as fd
import tkinter.messagebox as mb
from tree_agent.app import TreeAgentApp

root = tk.Tk()
app = TreeAgentApp(root, home=tempfile.mkdtemp(), single_instance=False)
proj = app.ws.projects[0]
menu_conv = proj["children"][0]
app.refresh_tree()
root.update()

captured = {}
real_popup = tk.Menu.tk_popup


def _spy(self, x, y, entry=""):
    captured["items"] = [
        self.entrycget(i, "label") if self.type(i) != "separator" else "---"
        for i in range(self.index("end") + 1)
    ]


class _Event:
    x_root = y_root = 0

    def __init__(self, y):
        self.y = y


tk.Menu.tk_popup = _spy
try:
    app.tree.selection_set(proj["id"]); app.tree.see(proj["id"]); root.update()
    app.on_tree_context_menu(_Event(app.tree.bbox(proj["id"])[1] + 2))
    items = captured["items"]
    assert any("匯出這個專案" in i for i in items), items
    assert any("匯出為 Markdown" in i for i in items), items
    assert any("匯入到這個專案" in i for i in items), items

    app.tree.selection_set(menu_conv["id"]); app.tree.see(menu_conv["id"]); root.update()
    app.on_tree_context_menu(_Event(app.tree.bbox(menu_conv["id"])[1] + 2))
    items = captured["items"]
    assert any("匯出這個對話" in i for i in items), items
    assert not any("匯入" in i for i in items), "importing into a conversation is meaningless"

    app.on_tree_context_menu(_Event(5000))          # empty space below the rows
    items = captured["items"]
    assert any("匯出整個工作區" in i for i in items), items
    assert any("匯入到最上層" in i for i in items), items
    assert items[0] != "---", "no leading separator on the empty-space menu"
finally:
    tk.Menu.tk_popup = real_popup

# the empty-space import must ignore whatever happens to be selected
app.tree.selection_set(proj["id"]); root.update()
fd.askopenfilename = lambda **k: archive
mb.askyesno = lambda *a, **k: True
mb.showinfo = lambda *a, **k: None
before_top, before_children = len(app.ws.projects), len(proj["children"])
app.import_archive(top_level=True)
assert len(app.ws.projects) == before_top + 1, "top_level must not use the selection"
assert len(proj["children"]) == before_children, "nothing should land under the selection"
print("context menu exposes export/import; top-level import ignores the selection OK")

app.on_close()

print("\nALL TRANSFER TESTS PASSED")
