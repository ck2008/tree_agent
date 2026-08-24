"""Which projects are open, and where you land after a delete.

Two bugs this locks down:

  * `<<TreeviewOpen>>` / `<<TreeviewClose>>` carry no item, and clicking the
    expander does not move the focus. Reading `tree.focus()` therefore recorded
    the *clicked* node's open state against whatever node happened to be
    focused, scattering wrong values across the whole tree over a session.
  * Deleting a node cleared the selection, so the detail pane went blank and the
    tree lost its place.
"""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import tempfile, tkinter as tk
import tkinter.messagebox as mb
from tree_agent import store
from tree_agent.app import TreeAgentApp

mb.askyesno = lambda *a, **k: True
mb.showinfo = lambda *a, **k: None

home = tempfile.mkdtemp()
root = tk.Tk()
app = TreeAgentApp(root, home=home, single_instance=False)
top = app.ws.projects[0]
alpha = app.ws.add_project(top["id"], "專案A")
beta = app.ws.add_project(top["id"], "專案B")
app.ws.add_conversation(alpha["id"], "對話A")
app.ws.add_conversation(beta["id"], "對話B")
app.refresh_tree()
root.update()


def flag(node):
    return app.ws.find(node["id"])["expanded"]


# ---- the clicked node is recorded, not the focused one ----
app.tree.focus(alpha["id"]); app.tree.selection_set(alpha["id"]); root.update()
app.tree.item(beta["id"], open=False)
app.on_tree_open_close()
assert flag(alpha) is True, f"the focused node must be left alone: {flag(alpha)!r}"
assert flag(beta) is False, f"the collapsed node must be recorded: {flag(beta)!r}"

# and the other way round
app.tree.focus(beta["id"]); root.update()
app.tree.item(alpha["id"], open=False)
app.tree.item(beta["id"], open=True)
app.on_tree_open_close()
assert flag(alpha) is False and flag(beta) is True, (flag(alpha), flag(beta))
# always a real bool, never Tk's 0/1
assert isinstance(flag(alpha), bool) and isinstance(flag(beta), bool)
print("open state recorded per node, as a bool OK")

# ---- it survives a reload ----
app.tree.item(alpha["id"], open=True); app.on_tree_open_close()
app.on_close()
root2 = tk.Tk()
app = TreeAgentApp(root2, home=home, single_instance=False)
root = root2
top = app.ws.projects[0]
alpha = next(n for n, _ in app.ws.walk() if n["name"] == "專案A")
beta = next(n for n, _ in app.ws.walk() if n["name"] == "專案B")
root.update()
assert bool(app.tree.item(alpha["id"], "open")) is True
assert bool(app.tree.item(beta["id"], "open")) is True
app.tree.item(beta["id"], open=False); app.on_tree_open_close()
app.on_close()
root3 = tk.Tk()
app = TreeAgentApp(root3, home=home, single_instance=False)
root = root3
beta = next(n for n, _ in app.ws.walk() if n["name"] == "專案B")
root.update()
assert bool(app.tree.item(beta["id"], "open")) is False, "collapse should persist"
print("expansion state persists across restarts OK")

# ---- a search filter force-opens everything; that must not be recorded ----
before = {n["name"]: n["expanded"] for n, _ in app.ws.walk() if n["kind"] == store.PROJECT}
app.search_var.set("對話"); app._apply_search(); root.update()
app.on_tree_open_close()          # as if you toggled something while filtering
after = {n["name"]: n["expanded"] for n, _ in app.ws.walk() if n["kind"] == store.PROJECT}
assert after == before, (before, after)
app.clear_search(); root.update()
print("filtering does not overwrite the real expansion state OK")

# ---- deleting keeps the tree open and lands on a neighbour ----
proj = app.ws.add_project(app.ws.projects[0]["id"], "3Dshot")
convs = [app.ws.add_conversation(proj["id"], f"對話{i}") for i in range(3)]
app.refresh_tree(); root.update()
app.tree.item(proj["id"], open=True); app.on_tree_open_close()

app._select(convs[1]["id"]); root.update()
app.delete_selected(); root.update()
assert app.ws.find(proj["id"])["expanded"] is True, "the parent must stay expanded"
assert bool(app.tree.item(proj["id"], "open")), "and stay open in the widget"
assert app.ws.find(app.current_id)["name"] == "對話2", "select the next sibling"
assert app.conv_view.conv_id == app.current_id, "and show it"
print("delete keeps the parent open and selects the next sibling OK")

# deleting the last child falls back to the previous sibling
app.delete_selected(); root.update()
assert app.ws.find(app.current_id)["name"] == "對話0", app.ws.find(app.current_id)["name"]
# and with no siblings left, to the parent project
app.delete_selected(); root.update()
assert app.ws.find(app.current_id)["name"] == "3Dshot", app.ws.find(app.current_id)["name"]
assert app.proj_view.project_id == app.current_id, "the project page should be showing"
print("falls back to previous sibling, then to the parent OK")

# deleting the very last top-level project leaves nothing selected, not a crash
for node in list(app.ws.projects):
    app._select(node["id"])
    app.delete_selected()
root.update()
assert app.ws.projects == [], [n["name"] for n in app.ws.projects]
assert app.current_id is None
print("deleting everything leaves an empty tree without erroring OK")

app.on_close()
print("\nALL TREE STATE TESTS PASSED")
