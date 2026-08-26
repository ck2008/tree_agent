"""Layout regressions seen on screen: reversed toolbar order, and header
buttons pushed off the right edge by a long project path."""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import tempfile, tkinter as tk
from tree_agent.app import TreeAgentApp

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
assert str(t.tag_cget("role_user", "justify")) == "right"
assert str(t.tag_cget("agent", "justify") or "left") == "left"
assert str(t.tag_cget("tool", "justify") or "left") == "left"
assert int(t.tag_cget("user", "lmargin1")) > 0, "right-aligned text needs a left gutter"
assert int(t.tag_cget("user", "rmargin")) > 0
# No background tint: Tk paints a tag background across the whole line,
# so it read as a full-width band rather than a bubble. Separation comes
# from alignment, spacing, and the label colour only.
assert str(t.tag_cget("user", "background")) == "", t.tag_cget("user", "background")
assert not t.tag_ranges("separator"), "no grey separator rule"
print("user right-aligned, Codex left-aligned OK")

# and it holds for what is actually on screen
idx = t.search("這裡不該被渲染", "1.0")
assert "user" in t.tag_names(idx), t.tag_names(idx)
idx = t.search("一、需求理解", "1.0")
assert "user" not in t.tag_names(idx), t.tag_names(idx)
print("live transcript carries the right tags OK")

app.on_close()
print("\nALL LAYOUT TESTS PASSED")
