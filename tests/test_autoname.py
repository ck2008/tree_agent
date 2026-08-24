"""Conversations title themselves from their first message.

Derived locally rather than asked of Codex: that would cost an extra turn, and
the opening message is already the best summary of intent.

The truncation rule comes from real data. First messages here typically look
like `"F:\\a\\b\\kws\\home_new.aspx" 有什麼作用` — plain front-truncation would
keep the drive letter and throw the actual question away, so path-looking tokens
are reduced to their file name first.
"""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import tempfile, tkinter as tk
from tree_agent import codex_runner as cr
from tree_agent import store
from tree_agent.app import TreeAgentApp, IMAGE_ONLY_PROMPT

# ---- the title rule ----
assert store.title_from('"F:\\eic_server\\eic_server_std_dev\\kws\\home_new.aspx" 有什麼作用') \
    == "home_new.aspx 有什麼作用"
assert store.title_from("scan project") == "scan project"
assert store.title_from("implement  3d shot game") == "implement 3d shot game", "whitespace collapses"
assert store.title_from("  多餘空白  ") == "多餘空白"
# UNC and forward-slash paths shrink too
assert store.title_from(r"\\server\share\deep\report.aspx 壞了") == "report.aspx 壞了"
assert store.title_from("/usr/local/share/something/config.json 是什麼") == "config.json 是什麼"
# a short token with a slash is prose, not a path
assert store.title_from("a/b 是什麼") == "a/b 是什麼"
# over-long titles are cut with an ellipsis, never silently
long = store.title_from("這是一個非常長的問題" * 6)
assert len(long) <= store.TITLE_MAX_CHARS + 1 and long.endswith("…"), long
assert store.title_from("") == "" and store.title_from(None) == ""
print("title rule keeps the question and shortens paths OK")

# ---- which names may be replaced ----
for auto in ("新對話", "新對話 2", "新對話1", "新對話  10"):
    assert store.is_auto_name(auto), auto
for kept in ("排查 401", "新對話 (分岔)", "程式碼審查", "", "新對話x", "我的新對話"):
    assert not store.is_auto_name(kept), kept
print("only placeholder names are considered replaceable OK")

# ---- the send path renames exactly once ----
root = tk.Tk()
app = TreeAgentApp(root, home=tempfile.mkdtemp(), single_instance=False)
proj = app.ws.projects[0]


class Fake(cr.Turn):
    def start(self):
        pass


real_turn = cr.Turn


def ask(conv, text, images=None):
    cr.Turn = Fake
    try:
        app.send(conv["id"], text, images=images)
    finally:
        cr.Turn = real_turn
    app._retire_turn(conv["id"])
    return app.ws.find(conv["id"])["name"]


def fresh():
    node = app.ws.add_conversation(proj["id"],
                                  app.ws.unique_name(proj, store.AUTO_CONVERSATION_NAME))
    app.refresh_tree()
    return node


first = proj["children"][0]
app._select(first["id"]); root.update()
assert ask(first, '"F:\\a\\b\\kws\\home_new.aspx" 有什麼作用') == "home_new.aspx 有什麼作用"
# the tree row and the header follow the rename
assert "home_new.aspx" in app.tree.item(first["id"], "text")
assert "home_new.aspx" in app.conv_view.title_label.cget("text")
print("renamed on the first message; tree and header updated OK")

# a later message must not re-title
app.ws.set_thread_id(first["id"], "01a-tid")
assert ask(first, "一個完全不同的後續問題") == "home_new.aspx 有什麼作用"
print("later messages leave the title alone OK")

# a name you typed is never overwritten
mine = app.ws.add_conversation(proj["id"], "我自己取的名字")
assert ask(mine, "隨便問一句") == "我自己取的名字"
# nor is a fork's name, nor the review action's
forked_like = app.ws.add_conversation(proj["id"], "排查 401 (分岔)")
assert ask(forked_like, "問題") == "排查 401 (分岔)"
review_like = app.ws.add_conversation(proj["id"], "程式碼審查")
assert ask(review_like, "問題") == "程式碼審查"
print("manual, forked and review names are all preserved OK")

# an image-only message titles itself from the image
shot = os.path.join(tempfile.mkdtemp(), "screenshot-abc.png")
open(shot, "wb").write(b"x")
assert ask(fresh(), IMAGE_ONLY_PROMPT, [shot]) == "screenshot-abc.png"
print("image-only message titled from the attachment OK")

# too little to work with -> keep the placeholder rather than a useless title
keeper = fresh()
assert store.is_auto_name(ask(keeper, "1")), app.ws.find(keeper["id"])["name"]
assert store.is_auto_name(ask(fresh(), "   "))
print("a message with nothing in it keeps the placeholder OK")

# two conversations opening the same way do not collide
a, b = fresh(), fresh()
assert ask(a, "scan project") == "scan project"
assert ask(b, "scan project") == "scan project 2"
names = [c["name"] for c in app.ws.find(proj["id"])["children"]]
assert len(names) == len(set(names)), names
print("colliding titles get a suffix OK:", names)

app.on_close()
print("\nALL AUTONAME TESTS PASSED")
