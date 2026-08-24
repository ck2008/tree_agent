"""Reading experience: measure width, message separation, follow-the-tail,
running progress, and cross-conversation search.

Two Tk facts these work around:
  * Text has no maximum-width option, so the container is padded instead.
    Unbounded, a maximised window gives ~237 latin characters per line.
  * A tag's background is painted across the whole line, not behind the glyphs,
    so a right-aligned "bubble" renders as a full-width band.
"""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import tempfile, time, tkinter as tk
from tree_agent import codex_runner as cr
from tree_agent.app import TreeAgentApp, MAX_CONTENT_PX, STAGE_LABELS

home = tempfile.mkdtemp()
root = tk.Tk()
root.geometry("1900x700")
app = TreeAgentApp(root, home=home, single_instance=False)
top = app.ws.projects[0]
conv = top["children"][0]
app.refresh_tree(); app._select(conv["id"])
root.update_idletasks(); root.update()
view = app.conv_view

# ---- the readable measure is capped, whatever the window size ----
# The cap applies to the text inside its pane; the pane itself is sized by the
# splitter sashes (see test_panels).
for width in (1900, 1400, 900):
    root.geometry(f"{width}x700")
    root.update_idletasks(); root.update()
    inset = int(view.text.cget("padx"))
    assert view.text.winfo_width() - 2 * inset <= MAX_CONTENT_PX, (width, inset)
root.geometry("1900x700"); root.update_idletasks(); root.update()
print(f"text measure capped at "
      f"{view.text.winfo_width() - 2 * int(view.text.cget('padx'))}px OK")

# ---- no full-width tinted band; messages are separated by a rule ----
assert str(view.text.tag_cget("user", "background")) == "", "the band must be gone"
assert str(view.text.tag_cget("user", "justify")) == "right", "alignment still marks it"
for i in range(3):
    app.ws.append_message(conv["id"], "agent" if i % 2 else "user", f"訊息 {i}")
view.show(app.ws.find(conv["id"])); root.update()
assert len(view.text.tag_ranges("separator")) // 2 == 2, "one rule between each pair"
assert view.text.get("1.0", "1.end").strip() != "", "no leading rule before the first"
print("band removed, separator rules between messages OK")

# ---- streaming follows the tail only when you are already at the tail ----
for i in range(300):
    app.ws.append_message(conv["id"], "agent", f"填充內容 {i}")
view.show(app.ws.find(conv["id"])); root.update()
assert view.following_tail(), view.text.yview()
assert not view.jump_button.winfo_ismapped(), "no jump button while at the tail"

view.append("agent", "在底部時應該跟隨")
root.update()
assert view.following_tail(), "at the tail, new output should follow"

view.text.yview_moveto(0.0); root.update()
assert not view.following_tail()
assert view.jump_button.winfo_ismapped(), "scrolled away -> offer a way back"
anchor = view.text.yview()[0]
view.append("agent", "讀歷史時不該被拽走")
view.append_log("stderr 也一樣")
root.update()
assert abs(view.text.yview()[0] - anchor) < 0.02, (anchor, view.text.yview()[0])
print("reading history is not interrupted by streaming output OK")

view.jump_to_latest(); root.update()
assert view.following_tail() and not view.jump_button.winfo_ismapped()
assert view.text.dlineinfo("end-1c") is not None, "the last line must be on screen"
print("jump to latest returns to the tail and hides the button OK")

# ---- running progress: elapsed seconds plus what it is doing ----
assert app.running_progress(conv["id"]) == "", "idle conversations show nothing"
assert not view.run_label.winfo_ismapped()


class FakeTurn:
    def __init__(self, *a, **k): pass
    def start(self): pass
    def cancel(self): pass


real_turn = cr.Turn
cr.Turn = FakeTurn
try:
    app.send(conv["id"], "跑一個")
finally:
    cr.Turn = real_turn

progress = app.running_progress(conv["id"])
assert progress.startswith("⏳ 執行中"), progress
assert STAGE_LABELS["start"] in progress, progress
view.refresh_progress(); root.update()
assert view.run_label.winfo_ismapped(), "the header should show it is running"

# the stage tracks the event stream
serial = app.turn_serials[conv["id"]]
app._handle_event(conv["id"], {"kind": "turn_start"})
assert STAGE_LABELS["thinking"] in app.running_progress(conv["id"])
app._handle_event(conv["id"], {"kind": "item", "role": "tool", "text": "$ dir"})
assert STAGE_LABELS["tool"] in app.running_progress(conv["id"])
app._handle_event(conv["id"], {"kind": "item", "role": "agent", "text": "答案"})
assert STAGE_LABELS["writing"] in app.running_progress(conv["id"])
print("progress reports:", app.running_progress(conv["id"]))

# the elapsed counter actually advances
app.turn_started[conv["id"]] -= 7          # pretend seven seconds passed
assert " 7s " in app.running_progress(conv["id"]), app.running_progress(conv["id"])
print("elapsed seconds counted OK")

app._handle_event(conv["id"], {"kind": "done", "returncode": 0})
assert app.running_progress(conv["id"]) == ""
view.refresh_progress(); root.update()
assert not view.run_label.winfo_ismapped(), "the indicator clears when the turn ends"
assert conv["id"] not in app.turn_started and conv["id"] not in app.turn_stage
print("indicator clears on completion OK")

# ---- cross-conversation search ----
alpha = app.ws.add_project(None, "客戶Alpha")
beta = app.ws.add_project(None, "客戶Beta")
c1 = app.ws.add_conversation(alpha["id"], "登入問題")
c2 = app.ws.add_conversation(beta["id"], "報表問題")
app.ws.append_message(c1["id"], "agent", "原因是 redirect_uri 設錯了")
app.ws.append_message(c2["id"], "agent", "報表的分頁邏輯有 off-by-one")
app.refresh_tree(); root.update()

def visible():
    out = []
    def walk(iid):
        for child in app.tree.get_children(iid):
            out.append(app.tree.item(child, "text"))
            walk(child)
    walk("")
    return out

everything = visible()
assert any("客戶Alpha" in t for t in everything) and any("客戶Beta" in t for t in everything)

# match on a conversation name
app.search_var.set("報表"); app._apply_search(); root.update()
shown = visible()
assert any("報表問題" in t for t in shown), shown
assert not any("登入問題" in t for t in shown), shown
assert any("客戶Beta" in t for t in shown), "the parent must stay so the hit is reachable"
assert not any("客戶Alpha" in t for t in shown), shown
print("search by conversation name OK:", [t.strip() for t in shown])

# match on transcript content only
app.search_var.set("redirect_uri"); app._apply_search(); root.update()
shown = visible()
assert any("登入問題" in t for t in shown), shown
assert not any("報表問題" in t for t in shown), shown
print("search inside transcripts OK:", [t.strip() for t in shown])

# match on a project name keeps its children
app.search_var.set("Alpha"); app._apply_search(); root.update()
shown = visible()
assert any("客戶Alpha" in t for t in shown) and any("登入問題" in t for t in shown), shown

# case-insensitive
app.search_var.set("ALPHA"); app._apply_search(); root.update()
assert any("客戶Alpha" in t for t in visible())

# no hits -> empty tree, not a crash
app.search_var.set("這個字串不存在於任何地方"); app._apply_search(); root.update()
assert visible() == [], visible()
assert "0 個項目符合" in app.status.cget("text"), app.status.cget("text")
print("no-hit search empties the tree and says so OK")

# clearing restores everything
app.clear_search(); root.update()
assert visible() == everything, (visible(), everything)
assert app.search_query == ""
print("clearing search restores the full tree OK")

app.turns.clear()
app.on_close()
print("\nALL READING TESTS PASSED")
