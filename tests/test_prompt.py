"""Per-project instructions for Codex.

Codex has no `--instructions` flag: its native mechanism is an `AGENTS.md` in
the working directory. Writing one would mean editing the user's repository and
would affect every other tool pointed at it, so these instructions ride along
with the first message of a new thread instead — after that the thread carries
them, and resending would only burn tokens.

Unlike cwd/model/sandbox, they *accumulate* down the tree: a client-wide rule
plus a subsystem-specific one is more useful than either replacing the other.
"""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import tempfile, tkinter as tk
from tree_agent import codex_runner as cr
from tree_agent import store
from tree_agent.app import TreeAgentApp

# ---- accumulation down the tree ----
ws = store.Workspace(tempfile.mkdtemp())
top = ws.projects[0]
ws.rename(top["id"], "客戶A")
ws.set_option(top["id"], "prompt", "一律用繁體中文回覆。")
mid = ws.add_project(top["id"], "認證中心")
ws.set_option(mid["id"], "prompt", "這個子系統用 OAuth2。")
deep = ws.add_project(mid["id"], "登入頁")            # deliberately no prompt
conv = ws.add_conversation(deep["id"], "排查 401")

assert ws.instructions_for(conv["id"]) == "一律用繁體中文回覆。\n\n這個子系統用 OAuth2。"
# root first, so the broad rule is stated before the narrow one
assert ws.instructions_for(conv["id"]).index("繁體") < ws.instructions_for(conv["id"]).index("OAuth2")
# a level with nothing to say contributes nothing, not a blank line
assert "\n\n\n" not in ws.instructions_for(conv["id"])
# include_self=False is what the form uses to describe the inherited part
assert ws.instructions_for(mid["id"], include_self=False) == "一律用繁體中文回覆。"
assert ws.instructions_for(top["id"], include_self=False) == ""
# whitespace-only prompts are ignored
ws.set_option(deep["id"], "prompt", "   \n  ")
assert ws.instructions_for(conv["id"]) == "一律用繁體中文回覆。\n\n這個子系統用 OAuth2。"
assert ws.instructions_for("no-such-node") == ""
print("instructions accumulate root-first and skip empty levels OK")

# survives a reload
ws.save()
assert store.Workspace(ws.home).instructions_for(conv["id"]).startswith("一律用繁體中文")
print("stored with the project OK")

# ---- injected on the first turn only ----
root = tk.Tk()
app = TreeAgentApp(root, home=tempfile.mkdtemp(), single_instance=False)
proj = app.ws.projects[0]
app.ws.set_option(proj["id"], "prompt", "用繁體中文，並且先看 web.config。")
target = proj["children"][0]
app.refresh_tree(); app._select(target["id"]); root.update()

sent: list[str] = []


class Spy(cr.Turn):
    def start(self):
        sent.append(self.prompt)


real_turn = cr.Turn
cr.Turn = Spy
try:
    app.send(target["id"], "為什麼會 401")
    app._retire_turn(target["id"])
    first = sent[-1]
    assert first.startswith("用繁體中文，並且先看 web.config。"), first
    assert first.endswith("為什麼會 401"), first
    assert "\n---\n" in first, "the instructions are separated from your question"

    # the transcript stores what you typed, plus a visible note
    messages = app.ws.find(target["id"])["messages"]
    assert messages[-1]["text"] == "為什麼會 401", messages[-1]
    assert messages[-2]["role"] == "meta" and "已套用" in messages[-2]["text"], messages[-2]
    assert "專案提示詞" in messages[-2]["text"]
    print("first turn carries the instructions; transcript records that it did OK")

    # once the thread exists they are not resent
    app.ws.set_thread_id(target["id"], "01a-existing")
    app.send(target["id"], "第二個問題")
    app._retire_turn(target["id"])
    assert sent[-1] == "第二個問題", sent[-1]
    assert app.ws.find(target["id"])["messages"][-2]["role"] != "meta" or \
        "已套用" not in app.ws.find(target["id"])["messages"][-2]["text"]
    print("later turns send only your message OK")

    # a fork inherits its source's context, so it must not re-inject either
    forked = app.ws.fork_conversation(target["id"])
    app.send(forked["id"], "分岔後的問題")
    app._retire_turn(forked["id"])
    assert sent[-1] == "分岔後的問題", sent[-1]
    print("a fork does not re-inject OK")

    # with no instructions anywhere, nothing is added and no note appears
    bare = app.ws.add_project(None, "沒有提示詞的專案")
    bare_conv = app.ws.add_conversation(bare["id"], "對話")
    app.send(bare_conv["id"], "直接問")
    app._retire_turn(bare_conv["id"])
    assert sent[-1] == "直接問", sent[-1]
    assert all(m["role"] != "meta" for m in app.ws.find(bare_conv["id"])["messages"])
    print("no instructions means no preamble and no note OK")
finally:
    cr.Turn = real_turn

# ---- the form shows, describes and saves it ----
child = app.ws.add_project(proj["id"], "子專案")
app.ws.set_option(child["id"], "prompt", "只讀不要改檔案。")
app.refresh_tree(); app._select(child["id"]); root.update()
view = app.proj_view
assert view.prompt_text.get("1.0", "end").strip() == "只讀不要改檔案。"
hint = view.prompt_hint.cget("text")
assert "上層" in hint and "web.config" in hint, hint
print("form shows its own prompt and previews the inherited part OK")

view.prompt_text.delete("1.0", "end")
view.prompt_text.insert("1.0", "  改過的內容  ")
view.save()
assert app.ws.find(child["id"])["prompt"] == "改過的內容", app.ws.find(child["id"])["prompt"]
view.prompt_text.delete("1.0", "end")
view.save()
assert app.ws.find(child["id"])["prompt"] is None, "clearing the box clears the setting"
hint = view.prompt_hint.cget("text")
assert "上層" in hint, hint
print("saving trims, and clearing removes the override OK")

# a project with no inherited instructions explains what the field does
app._select(proj["id"]); root.update()
assert "第一則訊息" in view.prompt_hint.cget("text"), view.prompt_hint.cget("text")
print("top-level project gets an explanatory hint OK")

# ---- the details panel shows what a conversation will actually run with ----
app._select(target["id"])
root.geometry("1900x760"); root.update_idletasks(); root.update()
# row names are ttk.Label, values are tk.Label, so collect both
from tkinter import ttk
labels = [c.cget("text") for c in app.conv_view.info_body.winfo_children()
          if isinstance(c, (tk.Label, ttk.Label))]
assert any("專案提示詞" in t for t in labels), labels
assert any("web.config" in t for t in labels), labels
print("details panel shows the effective instructions OK")

app.on_close()
print("\nALL PROMPT TESTS PASSED")
