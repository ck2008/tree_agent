"""Selecting and copying text out of the read-only transcript.

Why this needs testing at all: Tk denies keyboard focus to a `disabled` widget
unless `takefocus` is set explicitly, so Ctrl+C never reaches the transcript
even though the mouse can select text in it. And with no
`inactiveselectbackground`, the highlight disappears the moment focus moves to
the input box.
"""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import tempfile, tkinter as tk
from tree_agent.app import TreeAgentApp

home = tempfile.mkdtemp()
root = tk.Tk()
app = TreeAgentApp(root, home=home, single_instance=False)
conv = app.ws.projects[0]["children"][0]
app.ws.append_message(conv["id"], "user", "列出檔案清單")
app.ws.append_message(conv["id"], "agent", "目前工作目錄：L:\\eic_server\\eic_server_edit\\kws")
app.refresh_tree()
app._select(conv["id"])
root.update()

view = app.conv_view
text = view.text
assert str(text.cget("state")) == "disabled", "the transcript must stay read-only"

# ---- the widget can hold keyboard focus despite being disabled ----
assert text.cget("takefocus") in (1, "1", True), repr(text.cget("takefocus"))
assert str(text.cget("inactiveselectbackground")) != "", "selection must survive focus loss"
assert str(text.cget("selectbackground")) != ""
print("disabled transcript is focusable and keeps its highlight OK")

# ---- clicking hands focus to the transcript ----
view.input.focus_set(); root.update()
assert root.focus_get() is view.input
text.event_generate("<Button-1>", x=30, y=30)
root.update()
assert root.focus_get() is text, "clicking the transcript must focus it"
print("click focuses the transcript OK")

# ---- Ctrl+C copies the selection ----
needle = "eic_server_edit"
start = text.search(needle, "1.0")
assert start, "fixture text not found"
text.tag_remove("sel", "1.0", "end")
text.tag_add("sel", start, f"{start}+{len(needle)}c")
root.update()
root.clipboard_clear(); root.update()
text.event_generate("<Control-c>")
root.update()
assert root.clipboard_get() == needle, repr(root.clipboard_get())
assert "已複製" in app.status.cget("text"), app.status.cget("text")
print("Ctrl+C copies the selection OK")

# Ctrl+Insert is the other conventional copy key
root.clipboard_clear(); root.update()
text.event_generate("<Control-Insert>")
root.update()
assert root.clipboard_get() == needle
print("Ctrl+Insert copies too OK")

# ---- an already-sent bubble can select and copy only part of its text ----
bubble = view._inline_bubbles[0]
sent_needle = "出檔案"
sent_start = bubble.search(sent_needle, "1.0")
assert sent_start, "fixture user message not found in its bubble"
bubble.tag_add("sel", sent_start, f"{sent_start}+{len(sent_needle)}c")
root.clipboard_clear(); root.update()
bubble.focus_set(); root.update()
bubble.event_generate("<Control-c>")
root.update()
assert root.clipboard_get() == sent_needle, repr(root.clipboard_get())
view._copy_selected_or_block(bubble, "列出檔案清單")
assert root.clipboard_get() == sent_needle
print("sent-message bubbles copy only their selection OK")

# ---- copying with nothing selected says so instead of clearing the clipboard ----
text.tag_remove("sel", "1.0", "end")
root.update()
view.copy_selection()
assert root.clipboard_get() == sent_needle, "an empty copy must not wipe the clipboard"
assert "沒有選取" in app.status.cget("text"), app.status.cget("text")
print("empty selection leaves the clipboard alone OK")

# ---- Ctrl+A selects the whole transcript ----
view.select_all()
root.update()
selected = view.selection()
assert "列出檔案清單" in selected and "eic_server_edit" in selected, repr(selected[:120])
root.clipboard_clear()
view.copy_selection()
root.update()
assert "列出檔案清單" in root.clipboard_get()
print("Ctrl+A selects all, then copies OK")

# ---- the transcript is still not editable ----
before = text.get("1.0", "end")
text.focus_set()
text.event_generate("<Key>", keysym="x")
text.event_generate("<<Paste>>")
root.update()
assert text.get("1.0", "end") == before, "the transcript must remain read-only"
print("still read-only after typing and paste attempts OK")

# ---- Ctrl+Enter still sends while the transcript holds focus ----
sent = []
app.send = lambda cid, prompt, images=None, review=None: sent.append((cid, prompt))
view.input.insert("1.0", "從逐字稿送出")
text.focus_set()
text.event_generate("<Control-Return>")
root.update()
assert sent == [(conv["id"], "從逐字稿送出")], sent
print("Ctrl+Enter works from the transcript OK")

# Windows uses Ctrl+Shift as a common IME-switch shortcut.  The composer must
# not consume its modifier key presses, and modified Return must remain a
# normal Text-widget action rather than send the conversation.
assert view.input.bind("<Control-KeyPress-Shift_L>")
assert view.input.bind("<Control-KeyPress-Shift_R>")
assert view._pass_through_ime_shortcut(object()) is None
assert view._on_input_return(type("Event", (), {"state": 0x0001})()) is None
print("Ctrl+Shift IME shortcut and Shift+Enter both pass through OK")

# ---- the right-click menu is wired up ----
assert text.bind("<Button-3>"), "transcript needs a context menu"
assert view.input.bind("<Button-3>"), "input box needs a context menu"
print("context menus bound OK")

app.on_close()
print("\nALL COPY TESTS PASSED")
