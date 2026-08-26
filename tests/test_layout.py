"""Layout regressions seen on screen: reversed toolbar order, and header
buttons pushed off the right edge by a long project path."""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import tempfile, tkinter as tk
from tree_agent.app import (BUBBLE_MAX_RATIO, COLORS, ROLE_LABELS, TreeAgentApp)

home = tempfile.mkdtemp()
root = tk.Tk()
app = TreeAgentApp(root, home=home)
root.update()

# A collapsed sash saved by an older version must not hide the project tree on
# the next launch.
app._set_sash(0)
root.update_idletasks(); root.update()
assert app.paned.sashpos(0) >= 220, app.paned.sashpos(0)
print("saved collapsed project pane is restored to a visible width OK")

# ---- toolbar reads left-to-right ----
bar = app.tree.master.master.winfo_children()[0]
buttons = [w for w in bar.winfo_children() if isinstance(w, tk.ttk.Button)]
labels = sorted(buttons, key=lambda w: w.winfo_x())
texts = [w.cget("text") for w in labels]
assert texts == ["＋專案", "＋子專案", "＋對話"], texts
print("toolbar order OK:", " ".join(texts))

# ---- a long path must not clip the header buttons ----
deep = app.ws.projects[0]
for name in ("eic_server", "eic_server_std", "kws", "very_long_subproject_name_here"):
    deep = app.ws.add_project(deep["id"], name)
conv = app.ws.add_conversation(deep["id"], "新對話")
app.ws.set_option(deep["id"], "cwd", r"F:\eic_server\eic_server_std_dev\kws\some\deeper\path")
app.refresh_tree()
app._select(conv["id"])
root.update_idletasks()
root.update()

view = app.conv_view
header = view.title_label.master
btns = view._header_buttons
assert len(view.title_label.cget("text")) > 60, view.title_label.cget("text")

for width in (1180, 900, 700):
    root.geometry(f"{width}x760")
    root.update_idletasks(); root.update()
    header_right = header.winfo_x() + header.winfo_width()
    btn_right = btns.winfo_x() + btns.winfo_width()
    assert btns.winfo_width() >= btns.winfo_reqwidth(), (
        f"@{width}: buttons squashed {btns.winfo_width()} < {btns.winfo_reqwidth()}"
    )
    assert btn_right <= header_right, f"@{width}: buttons clipped ({btn_right} > {header_right})"
    assert view.title_label.cget("wraplength") > 0, f"@{width}: wraplength not applied"
    print(f"  width={width}: buttons fit (right edge {btn_right} <= {header_right}), "
          f"wraplength={view.title_label.cget('wraplength')}")
print("header never clips its buttons OK")

# ---- agent replies render as Markdown in the live transcript ----
app.ws.append_message(conv["id"], "agent", "### 一、需求理解\n\n您想了解 **home_new.aspx** 的用途。")
view.show(app.ws.find(conv["id"]))
root.update()
body = view.text.get("1.0", "end")
assert "###" not in body and "**" not in body, repr(body)
idx = view.text.search("一、需求理解", "1.0")
assert "md_h3" in view.text.tag_names(idx), view.text.tag_names(idx)
idx = view.text.search("home_new.aspx", "1.0")
assert "md_bold" in view.text.tag_names(idx), view.text.tag_names(idx)

# the user's own text stays verbatim
app.ws.append_message(conv["id"], "user", "**這裡不該被渲染**")
view.show(app.ws.find(conv["id"]))
root.update()
assert "**這裡不該被渲染**" in view.text.get("1.0", "end")
print("agent Markdown rendered, user text verbatim OK")

# ---- your messages sit on the right, Codex's on the left ----
t = view.text
assert str(t.tag_cget("user", "justify")) == "right", t.tag_cget("user", "justify")
assert str(t.tag_cget("agent", "justify") or "left") == "left"
assert str(t.tag_cget("tool", "justify") or "left") == "left"
assert int(t.tag_cget("user", "lmargin1")) > 0, "right-aligned text needs a left gutter"
assert int(t.tag_cget("user", "rmargin")) > 0
# The tint lives on the embedded bubble, never on the tag: Tk paints a tag
# background across the whole display line, which read as a full-width band.
assert str(t.tag_cget("user", "background")) == "", t.tag_cget("user", "background")
assert not t.tag_ranges("separator"), "no grey separator rule"
print("user right-aligned, Codex left-aligned OK")

# ---- your message is a right-aligned bubble with no "你" label above it ----
root.update_idletasks(); root.update()
assert ROLE_LABELS["user"] not in t.get("1.0", "end"), "the 你 label is gone"
bubble = view._inline_bubbles[-1]
assert bubble.cget("text") == "**這裡不該被渲染**", bubble.cget("text")
assert str(bubble.cget("bg")) == COLORS["user_bg"], bubble.cget("bg")
# it hugs the right edge rather than spanning the measure
offset = bubble.winfo_rootx() - t.winfo_rootx()
assert offset > t.winfo_width() // 2, (offset, t.winfo_width())
assert offset + bubble.winfo_width() <= t.winfo_width(), "the bubble must stay inside"
assert bubble.winfo_width() <= int(t.winfo_width() * BUBBLE_MAX_RATIO) + 40,     (bubble.winfo_width(), t.winfo_width())
print(f"user bubble {bubble.winfo_width()}px at x={offset} of {t.winfo_width()}px OK")

# a Label holds no transcript text, so the words are also inserted elided --
# that is what keeps Ctrl+A, Ctrl+C and search working over your own messages
# `search` skips elided ranges unless asked, hence elide=True here
idx = t.search("這裡不該被渲染", "1.0", elide=True)
assert "user_hidden" in t.tag_names(idx), t.tag_names(idx)
assert str(t.tag_cget("user_hidden", "elide")) in ("1", "true", "True")
view.select_all()
assert "這裡不該被渲染" in view.selection(), "select-all must still reach your messages"
idx = t.search("一、需求理解", "1.0")
assert "user_hidden" not in t.tag_names(idx), t.tag_names(idx)
print("live transcript carries the right tags OK")

# ---- every embedded block shares one right edge, at any width ----
# The bubble is right-aligned to the readable measure. A card sized to the
# *pane* instead overshot that measure on a wide window -- it was clipped at
# the inset, and the bubbles then looked 400px short of the right edge.
app.ws.append_message(conv["id"], "agent", "則用：\n\n```bash\nnpx dsh web --host 127.0.0.1\n```")
app.ws.append_message(conv["id"], "user", "先升級node")
app.ws.append_message(conv["id"], "user",
                      "npm WARN EBADENGINE\nError: listen EACCES\n    at Server.listen (node:net:1)")
view.show(app.ws.find(conv["id"]))
for geometry, info in (("1900x900", True), ("1900x900", False), ("1100x800", False)):
    app.show_info_var.set(info)
    app.apply_panel_prefs()
    root.geometry(geometry)
    root.update_idletasks(); root.update()
    measure = t.winfo_width() - int(t.cget("padx"))
    blocks = (view._inline_bubbles + view._inline_code_widgets
              + view._inline_log_widgets)
    assert blocks, "fixture produced no embedded blocks"
    edges = {w.winfo_rootx() - t.winfo_rootx() + w.winfo_width() - measure for w in blocks}
    assert edges == {-14}, (geometry, info, sorted(edges), measure)
print("bubbles, code cards and log blocks share one right edge OK")

app.on_close()
print("\nALL LAYOUT TESTS PASSED")
