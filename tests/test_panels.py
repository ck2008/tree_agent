"""The two gutters left over by the width cap: an outline rail and a details panel.

The outline is built by querying tags already in the transcript (`role_user`,
`md_h1..3`) rather than threading positions through the renderer.

Both rails fold away when the window cannot spare the width, so the transcript
never gets squeezed below a readable measure — the details panel goes first,
because the outline is the navigation aid.
"""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import tempfile, tkinter as tk
from tree_agent.app import (TreeAgentApp, MAX_CONTENT_PX, MIN_CONTENT_PX,
                            OUTLINE_WIDTH, INFO_WIDTH, TEXT_INSET_PX)


def pump(win, times=5):
    for _ in range(times):
        win.update_idletasks()
        win.update()


def measure(view):
    """The readable width: the widget fills its pane, the text is inset."""
    return view.text.winfo_width() - 2 * int(view.text.cget("padx"))


home = tempfile.mkdtemp()
root = tk.Tk()
app = TreeAgentApp(root, home=home, single_instance=False)
conv = app.ws.projects[0]["children"][0]
app.ws.set_thread_id(conv["id"], "01a-panel-demo")
app.ws.append_message(conv["id"], "user", "列出檔案清單\n第二行不該進大綱")
app.ws.append_message(conv["id"], "agent",
                      "### 一、需求理解\n\n你要我列出檔案。\n\n### 二、待確認資訊\n\n請確認範圍。")
app.ws.append_message(conv["id"], "tool", "檔案變更\n  update home_new.aspx\n  add    config.js")
app.ws.append_message(conv["id"], "user", "1")
app.ws.append_message(conv["id"], "agent", "## 結果\n\n共 25 個資料夾。")
app.ws.add_usage(conv["id"], {"input_tokens": 1234, "output_tokens": 56})
app.refresh_tree()
app._select(conv["id"])
# geometry after construction: the app restores its own saved size on startup
root.geometry("1900x760")
root.update_idletasks()
root.update()
view = app.conv_view

# ---- both rails are shown at their asked-for width, transcript still capped ----
assert view._panels_shown == (True, True), view._panels_shown
assert len(view.splitter.panes()) == 3, view.splitter.panes()
assert view.outline.winfo_width() == OUTLINE_WIDTH, view.outline.winfo_width()
assert view.info.winfo_width() == INFO_WIDTH, view.info.winfo_width()
# the text keeps a readable measure inside whatever the pane gives it
assert measure(view) <= MAX_CONTENT_PX, measure(view)
print(f"rails {OUTLINE_WIDTH}/{INFO_WIDTH}px, measure {measure(view)}px OK")

# ---- the outline reflects the transcript, in document order ----
entries = view.outline_entries()
labels = [label for label, _, _ in entries]
assert labels == ["列出檔案清單", "一、需求理解", "二、待確認資訊", "1", "結果"], labels
# a multi-line question contributes only its first line
assert "第二行不該進大綱" not in labels
# your turns are top level, headings are nested by their level
depths = [depth for _, depth, _ in entries]
assert depths == [0, 3, 3, 0, 2], depths
# every entry points at a real transcript position, and they run in order
positions = [tuple(map(int, index.split("."))) for _, _, index in entries]
assert positions == sorted(positions), positions
print("outline entries, depths and order OK:", labels)

# clicking one scrolls the transcript to it
for _ in range(60):
    app.ws.append_message(conv["id"], "agent", "填充內容")
view.show(app.ws.find(conv["id"]))
root.update()
assert view.following_tail()
first_index = view.outline_entries()[0][2]
view.jump_to(first_index)
root.update()
assert not view.following_tail(), "jumping to the top should leave the tail"
top_line = int(view.text.index("@0,0").split(".")[0])
assert top_line <= int(first_index.split(".")[0]) + 1, (top_line, first_index)
print("clicking an outline entry scrolls there OK")

# ---- the details panel carries what the header line could not fit ----
texts = [c.cget("text") for c in view.info_body.winfo_children() if isinstance(c, tk.Label)]
joined = "\n".join(texts)
assert "01a-panel-demo" in joined, texts
assert "no-sandbox" in joined, texts
assert "1,234" in joined, "usage is broken out with thousands separators"
assert any("home_new.aspx" in t for t in texts), "files Codex touched are listed"
assert any("config.js" in t for t in texts), texts
print("details panel lists settings, usage and changed files OK")

# ---- narrow windows drop the details panel first, then the outline ----
seen = {}
for width in (1900, 1200, 1000, 820):
    root.geometry(f"{width}x760")
    root.update_idletasks(); root.update()
    seen[width] = view._panels_shown
    # whatever is shown, the transcript keeps a usable measure
    assert view.body.winfo_width() >= min(MIN_CONTENT_PX, view.winfo_width()) - 60, \
        (width, view.body.winfo_width())
print("panel visibility by width:", seen)
assert seen[1900] == (True, True)
assert seen[1000][1] is False, "the details panel yields first"
assert seen[1000][0] is True, "the outline is kept longer"

root.geometry("1900x760"); root.update_idletasks(); root.update()
assert view._panels_shown == (True, True), "and they come back when there is room"
print("rails fold away on narrow windows and return OK")

# ---- the preference is honoured and remembered ----
app.show_outline_var.set(False)
app.apply_panel_prefs()
root.update_idletasks(); root.update()
assert view._panels_shown == (False, True), view._panels_shown
assert len(view.splitter.panes()) == 2, view.splitter.panes()
assert measure(view) <= MAX_CONTENT_PX

app.show_info_var.set(False)
app.apply_panel_prefs()
root.update_idletasks(); root.update()
assert view._panels_shown == (False, False), view._panels_shown
assert len(view.splitter.panes()) == 1, view.splitter.panes()
# the pane now spans everything, but the text stays at a readable measure
assert view.body.winfo_width() > MAX_CONTENT_PX, view.body.winfo_width()
assert measure(view) <= MAX_CONTENT_PX, measure(view)
print("with both rails off the text is still capped inside a full-width pane OK")

# ---- the edge you can see is the edge you can drag ----
# The measure cap used to pad *around* the text widget, which left the visible
# edge of the transcript ~180px inside the pane — you would grab there and find
# nothing. Insetting the text within the widget keeps the two together.
app.show_outline_var.set(True); app.show_info_var.set(True)
app.limit_width_var.set(True)
app.apply_panel_prefs()
root.geometry("1900x760"); pump(root)


def offset(widget):
    return widget.winfo_rootx() - root.winfo_rootx()


sash0 = offset(view.splitter) + view.splitter.sashpos(0)
sash1 = offset(view.splitter) + view.splitter.sashpos(1)
assert offset(view.text) - sash0 < 12, (offset(view.text), sash0)
scrollbar_right = offset(view._vsb) + view._vsb.winfo_width()
assert sash1 - scrollbar_right < 12, (sash1, scrollbar_right)
# the widget fills its pane; only the text inside it is inset
assert view.text.winfo_width() >= view.body.winfo_width() - 24, \
    (view.text.winfo_width(), view.body.winfo_width())
assert measure(view) <= MAX_CONTENT_PX, measure(view)
print(f"transcript edge sits on the sash (gap {offset(view.text) - sash0}px), "
      f"measure {measure(view)}px OK")

# and it stays that way after a drag
view.splitter.sashpos(0, 420); pump(root)
moved = offset(view.splitter) + view.splitter.sashpos(0)
assert offset(view.text) - moved < 12, (offset(view.text), moved)
print("edge follows the sash when dragged OK")

# switching the cap off removes the inset entirely
app.limit_width_var.set(False); app.apply_panel_prefs(); pump(root)
assert int(view.text.cget("padx")) == TEXT_INSET_PX, view.text.cget("padx")
app.limit_width_var.set(True); app.apply_panel_prefs(); pump(root)
assert int(view.text.cget("padx")) > TEXT_INSET_PX, view.text.cget("padx")
print("cap toggles the inset OK")

# ---- every boundary is draggable, and the widths are remembered ----
app.show_outline_var.set(True); app.show_info_var.set(True)
app.apply_panel_prefs(); pump(root)
assert len(view.splitter.panes()) == 3, view.splitter.panes()

view.splitter.sashpos(0, 420); pump(root); view._remember_rail_widths()
assert view.outline.winfo_width() == 420, view.outline.winfo_width()
assert app.outline_width == 420, app.outline_width

total = view.splitter.winfo_width()
view.splitter.sashpos(1, total - 460); pump(root); view._remember_rail_widths()
assert abs(view.info.winfo_width() - 460) <= 8, view.info.winfo_width()
assert abs(app.info_width - view.info.winfo_width()) <= 1, app.info_width
# the transcript pane gave up the space, and its text is still capped
assert measure(view) <= MAX_CONTENT_PX, measure(view)
print(f"sashes dragged to {app.outline_width}/{app.info_width}px OK")

# hiding and reshowing a rail must not lose the width you chose
app.show_outline_var.set(False); app.apply_panel_prefs(); pump(root)
app.show_outline_var.set(True); app.apply_panel_prefs(); pump(root)
assert view.outline.winfo_width() == 420, view.outline.winfo_width()
print("rail width survives hide/show OK")

dragged_outline, dragged_info = app.outline_width, app.info_width
app.on_close()

root_drag = tk.Tk()
app_drag = TreeAgentApp(root_drag, home=home, single_instance=False)
app_drag._select(app_drag.ws.projects[0]["children"][0]["id"])
root_drag.geometry("1900x760"); pump(root_drag)
assert app_drag.outline_width == dragged_outline, app_drag.outline_width
assert app_drag.conv_view.outline.winfo_width() == dragged_outline
assert abs(app_drag.conv_view.info.winfo_width() - dragged_info) <= 2
assert measure(app_drag.conv_view) <= MAX_CONTENT_PX
print("dragged widths persist across a restart OK")

# turn both rails off so the next restart check starts from a known state
app_drag.show_outline_var.set(False)
app_drag.show_info_var.set(False)
app_drag.apply_panel_prefs()
app_drag.on_close()

root2 = tk.Tk()
app2 = TreeAgentApp(root2, home=home, single_instance=False)
root2.update_idletasks(); root2.update()
assert app2.show_outline is False and app2.show_info is False, (app2.show_outline, app2.show_info)
assert app2.show_outline_var.get() is False
app2.show_outline_var.set(True)
app2.apply_panel_prefs()
app2.on_close()
root3 = tk.Tk()
app3 = TreeAgentApp(root3, home=home, single_instance=False)
assert app3.show_outline is True and app3.show_info is False
print("panel preferences persist across restarts OK")

# ---- an empty conversation says so instead of showing a blank rail ----
empty = app3.ws.add_conversation(app3.ws.projects[0]["id"], "空的")
app3.refresh_tree(); app3._select(empty["id"])
root3.geometry("1900x760"); root3.update_idletasks(); root3.update()
assert app3.conv_view.outline_entries() == []
rail = [c.cget("text") for c in app3.conv_view.outline_body.winfo_children()]
assert any("還沒有內容" in t for t in rail), rail
print("empty conversation shows a placeholder in the rail OK")

app3.on_close()
print("\nALL PANEL TESTS PASSED")
