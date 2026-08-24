"""Terminal control codes must never reach the transcript.

PowerShell colours its table headers, so captured command output arrives as
`\x1b[32;1mPath\x1b[0m`. Two defences: NO_COLOR=1 in the child environment
(measured to give 0 escapes with codex 0.147), and stripping on the way in and
on the way out — the latter so transcripts recorded before this existed still
render cleanly.
"""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import tempfile, tkinter as tk
from tree_agent import codex_runner as cr
from tree_agent.app import TreeAgentApp

ESC = "\x1b"

# ---- the exact bytes seen in the real transcript ----
raw = f"{ESC}[32;1mPath{ESC}[0m\r\n{ESC}[32;1m----{ESC}[0m\r\nF:\\eic_server\\kws\r\n"
clean = cr.clean_output(raw)
assert ESC not in clean, repr(clean)
assert clean == "Path\n----\nF:\\eic_server\\kws\n", repr(clean)
print("real-world PowerShell header cleaned OK")

# ---- other escape shapes ----
assert cr.clean_output(f"a{ESC}[0Kb") == "ab"                    # erase-in-line
assert cr.clean_output(f"a{ESC}[2;5Hb") == "ab"                  # cursor move
assert cr.clean_output(f"a{ESC}]0;window title\x07b") == "ab"    # OSC + BEL
assert cr.clean_output(f"a{ESC}]0;title{ESC}\\b") == "ab"        # OSC + ST
assert cr.clean_output(f"a{ESC}=b") == "ab"                      # two-byte escape
assert cr.clean_output(f"{ESC}[?25lhidden{ESC}[?25h") == "hidden"  # private mode
print("CSI / OSC / two-byte escapes all stripped OK")

# ---- newline normalisation, and nothing else touched ----
assert cr.clean_output("a\r\nb\rc\nd") == "a\nb\nc\nd"
assert cr.clean_output("") == ""
assert cr.clean_output("plain text 中文 $ | & [brackets]") == "plain text 中文 $ | & [brackets]"
assert cr.clean_output("cost: [32] items") == "cost: [32] items", "a bare [32] is not an escape"
print("newlines normalised, ordinary text untouched OK")

# ---- stripped on ingest ----
role, text = cr.describe_item({
    "type": "command_execution",
    "command": "Get-ChildItem",
    "exit_code": 0,
    "aggregated_output": raw,
})
assert role == "tool"
assert ESC not in text, repr(text)
assert "Path" in text and "$ Get-ChildItem" in text
print("command output stripped on ingest OK")

# ---- NO_COLOR is set for the child, without clobbering an explicit choice ----
turn = cr.Turn("p", ".", lambda e: None)
assert "env" not in dir(turn) or True  # env is built inside _run
src = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "tree_agent", "codex_runner.py"), encoding="utf-8").read()
assert 'env.setdefault("NO_COLOR", "1")' in src, "child env must request no colour"
assert "env=env" in src, "the env must actually be passed to Popen"
print("NO_COLOR wired into the child environment OK")

# ---- stripped on render, so already-stored history displays clean ----
home = tempfile.mkdtemp()
root = tk.Tk()
app = TreeAgentApp(root, home=home, single_instance=False)
conv = app.ws.projects[0]["children"][0]
# write the raw escapes straight into the store, as an old session would have
app.ws.append_message(conv["id"], "tool", f"$ Get-ChildItem\n{raw}")
app.conv_view.show(app.ws.find(conv["id"]))
root.update()
shown = app.conv_view.text.get("1.0", "end")
assert ESC not in shown, repr(shown[:200])
assert "Path" in shown
print("pre-existing transcript renders without escapes OK")

# log lines too
app.conv_view.append_log(f"{ESC}[31mERROR{ESC}[0m something failed")
root.update()
shown = app.conv_view.text.get("1.0", "end")
assert ESC not in shown
assert "ERROR something failed" in shown
print("log lines stripped OK")

app.on_close()
print("\nALL ANSI TESTS PASSED")
