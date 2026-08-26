"""The View-menu theme switch updates live widgets and persists per workspace."""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import tempfile, tkinter as tk
from tkinter import ttk
from tree_agent.app import (
    COLORS, DARK_COLORS, LIGHT_COLORS, THEME_DARK, THEME_LIGHT, TreeAgentApp,
)

home = tempfile.mkdtemp()
root = tk.Tk()
app = TreeAgentApp(root, home=home, single_instance=False)

# A fresh workspace is deliberately conservative: the previous light UI stays
# the default until the user opts in through View > 深色模式.
assert app.theme == THEME_LIGHT, app.theme
assert COLORS == LIGHT_COLORS, COLORS

# A bubble already on screen must follow the theme. Its light tint is shared
# with `hover` and `button_hover`, so the value-for-value palette swap alone
# would repaint it as the wrong Dark+ layer.
conv = app.ws.projects[0]["children"][0]
app.ws.append_message(conv["id"], "user", "測試訊息")
app.refresh_tree(); app._select(conv["id"])
root.update_idletasks(); root.update()
bubble = app.conv_view._inline_bubbles[0]
assert str(bubble.cget("bg")) == LIGHT_COLORS["user_bg"], bubble.cget("bg")

app.theme_var.set(THEME_DARK)
app.apply_theme()
root.update()
assert str(bubble.cget("bg")) == DARK_COLORS["user_bg"], bubble.cget("bg")
assert str(bubble.cget("fg")) == DARK_COLORS["text"], bubble.cget("fg")
assert str(bubble.master.cget("bg")) == DARK_COLORS["user_bg"]
print("your message bubble follows the theme OK")
assert app.theme == THEME_DARK
assert COLORS == DARK_COLORS, COLORS
assert app.ws.data["ui"]["theme"] == THEME_DARK
assert str(app.search_entry.cget("background")) == DARK_COLORS["input"]
assert str(app.conv_view.text.cget("background")) == DARK_COLORS["editor"]
assert str(app.tree_scrollbar.cget("style")) == "VS.Vertical.TScrollbar"
assert str(app.conv_view._vsb.cget("style")) == "VS.Vertical.TScrollbar"
style = ttk.Style(root)
assert style.lookup("Primary.TButton", "foreground", state=("disabled",)) == DARK_COLORS["text"]
assert app.tree.tag_configure("conversation")["foreground"] == DARK_COLORS["tree_conversation"]
assert str(app._menus[0].cget("background")) == DARK_COLORS["panel"]
print("dark mode updates classic widgets, transcript tags, and tree tags OK")

# The dark chrome is flat: rail, menu bar, both rails and the transcript share
# one surface, and only a raised card lifts off it. The value-for-value repaint
# cannot tell those roles apart -- they share a value -- so apply_theme names
# them; this is what catches it if one is forgotten.
flat = {
    "root": str(root.cget("bg")),
    "menu bar": str(app.custom_menu_bar.cget("bg")),
    "menu button": str(app.custom_menu_buttons[0].cget("bg")),
    "activity rail": str(app.activity_rail.cget("bg")),
    "rail button": str(app.activity_button.cget("bg")),
    "outline": str(app.conv_view.outline.cget("bg")),
    "info": str(app.conv_view.info.cget("bg")),
    "transcript": str(app.conv_view.text.cget("bg")),
}
surface = DARK_COLORS["editor"]
assert set(flat.values()) == {surface}, flat
style = ttk.Style(root)
for name, option in (("Sidebar.TFrame", "background"), ("Panel.TFrame", "background"),
                     ("Treeview", "background"), ("Treeview", "fieldbackground")):
    assert style.lookup(name, option) == surface, (name, option, style.lookup(name, option))
# ...and the cards are still distinguishable from it
assert DARK_COLORS["tool_bg"] != surface and DARK_COLORS["user_bg"] != surface
print(f"dark chrome is flat at {surface}, cards raised OK")

app.on_close()
root2 = tk.Tk()
app2 = TreeAgentApp(root2, home=home, single_instance=False)
assert app2.theme == THEME_DARK
assert COLORS == DARK_COLORS
print("dark-mode preference persists across restarts OK")

app2.theme_var.set(THEME_LIGHT)
app2.apply_theme()
root2.update()
assert COLORS == LIGHT_COLORS
assert str(app2.search_entry.cget("background")) == LIGHT_COLORS["input"]
# switching back must undo the flattening, not leave the chrome one colour
assert str(app2.custom_menu_bar.cget("bg")) == LIGHT_COLORS["toolbar"]
assert str(app2.activity_rail.cget("bg")) == LIGHT_COLORS["activity"]
assert str(app2.conv_view.text.cget("bg")) == LIGHT_COLORS["editor"]
assert str(app2.conv_view.outline.cget("bg")) == LIGHT_COLORS["sidebar"]
style = ttk.Style(root2)
assert style.lookup("Primary.TButton", "background", state=("disabled",)) == LIGHT_COLORS["tool_bg"]
assert style.lookup("Primary.TButton", "foreground", state=("disabled",)) == LIGHT_COLORS["text"]
assert style.lookup("Primary.TButton", "foreground") == LIGHT_COLORS["text"]
print("switching back to light mode updates live widgets OK")

app2.on_close()
print("\nALL THEME TESTS PASSED")
