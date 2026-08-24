"""Creating a conversation should be one click: no name prompt, and you land
in it ready to type.

A conversation is identified by what is in it, so being asked to name it before
you know what it is about is pure friction. Projects still prompt — a project is
a container you live with, and it carries the cwd/model/sandbox settings.
"""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import tempfile, tkinter as tk
import tkinter.messagebox as mb
import tkinter.simpledialog as sd
from tree_agent import store
from tree_agent.app import TreeAgentApp

# Any dialog at all during conversation creation is a failure, so make one fatal.
asked = []


def _forbidden(*a, **k):
    asked.append(a)
    raise AssertionError("a conversation must not prompt for anything")


infos = []
mb.showinfo = lambda t, m, **k: infos.append(m)

home = tempfile.mkdtemp()
root = tk.Tk()
app = TreeAgentApp(root, home=home, single_instance=False)
proj = app.ws.projects[0]
app.refresh_tree()
app._select(proj["id"])
root.update()

real_askstring = sd.askstring
sd.askstring = _forbidden
try:
    # ---- one click, no dialog, and you end up inside it ----
    before = len(proj["children"])
    app.new_conversation()
    root.update()
    assert not asked, asked
    assert len(proj["children"]) == before + 1
    created = proj["children"][-1]
    assert app.current_id == created["id"], (app.current_id, created["id"])
    assert app.conv_view.conv_id == created["id"]
    assert app.tree.selection() == (created["id"],), app.tree.selection()
    assert root.focus_get() is app.conv_view.input, "the caret should be in the input box"
    assert created["name"] in app.status.cget("text"), app.status.cget("text")
    print(f"created {created['name']!r} with no dialog, selected and focused OK")

    # ---- names stay distinguishable instead of piling up identical ----
    app.new_conversation(); root.update()
    app.new_conversation(); root.update()
    names = [c["name"] for c in proj["children"] if c["kind"] == store.CONVERSATION]
    assert names[-3:] == ["新對話 2", "新對話 3", "新對話 4"], names
    print("auto-naming:", names)

    # ---- selecting a conversation creates the next one as its sibling ----
    sibling_of = proj["children"][-1]
    app._select(sibling_of["id"]); root.update()
    app.new_conversation(); root.update()
    assert app.ws.parent_of(app.current_id)["id"] == proj["id"], "same project"
    print("creating from a conversation adds a sibling OK")

    # ---- a live search filter must not hide what you just made ----
    app.search_var.set("這個字串不存在"); app._apply_search(); root.update()
    assert app.tree.get_children("") == (), "filter should have emptied the tree"
    app._select(proj["id"]) if app.tree.exists(proj["id"]) else None
    app.current_id = proj["id"]          # the filter hid it, but it is still current
    app.new_conversation(); root.update()
    assert app.search_query == "", "creating should lift a filter that would hide it"
    assert app.search_var.get() == ""
    assert app.tree.exists(app.current_id), "the new conversation must be visible"
    print("search filter lifted so the new conversation is visible OK")
finally:
    sd.askstring = real_askstring

# ---- with nothing selected it explains instead of guessing ----
app.tree.selection_remove(*app.tree.selection())
app.current_id = None
sd.askstring = _forbidden
try:
    before_total = sum(1 for n, _ in app.ws.walk() if n["kind"] == store.CONVERSATION)
    app.new_conversation()
    after_total = sum(1 for n, _ in app.ws.walk() if n["kind"] == store.CONVERSATION)
    assert after_total == before_total, "nothing should be created"
    assert infos and "請先選" in infos[-1], infos
finally:
    sd.askstring = real_askstring
print("no selection -> explains, creates nothing OK")

# ---- projects deliberately still ask for a name ----
prompts = []
sd.askstring = lambda title, prompt, **k: prompts.append(title) or "自訂專案名"
try:
    app._select(proj["id"]); root.update()
    app.new_project(top_level=False)
    root.update()
finally:
    sd.askstring = real_askstring
assert prompts, "a project should still be named up front"
assert any(c["name"] == "自訂專案名" for c in proj["children"]), \
    [c["name"] for c in proj["children"]]
assert app.ws.find(app.current_id)["name"] == "自訂專案名", "and be selected"
print("projects still prompt, and the new one is selected OK")

# cancelling the project prompt creates nothing
sd.askstring = lambda *a, **k: None
try:
    before = len(proj["children"])
    app.new_project(top_level=False)
    assert len(proj["children"]) == before
finally:
    sd.askstring = real_askstring
print("cancelling the project prompt creates nothing OK")

app.on_close()
print("\nALL CREATE TESTS PASSED")
