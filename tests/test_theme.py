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

app.theme_var.set(THEME_DARK)
app.apply_theme()
root.update()
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
style = ttk.Style(root2)
assert style.lookup("Primary.TButton", "background", state=("disabled",)) == LIGHT_COLORS["tool_bg"]
assert style.lookup("Primary.TButton", "foreground", state=("disabled",)) == LIGHT_COLORS["text"]
assert style.lookup("Primary.TButton", "foreground") == LIGHT_COLORS["text"]
print("switching back to light mode updates live widgets OK")

app2.on_close()
print("\nALL THEME TESTS PASSED")
