"""Workspace defaults: where new projects start, and how much Codex may do.

Codex's own default sandbox is `workspace-write`, but that cannot run a single
command when the working directory is on a mapped network drive — which is where
the repositories this tool is pointed at actually live. So the shipped default is
the full-access one, and every project can still override it.
"""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import tempfile, tkinter as tk
from tree_agent import codex_runner as cr
from tree_agent import store
from tree_agent.app import TreeAgentApp, INHERIT

# ---- the shipped default is the full-access sandbox ----
assert store.DEFAULT_SANDBOX == "no-sandbox", store.DEFAULT_SANDBOX
assert store.DEFAULT_SANDBOX in cr.SANDBOX_MODES
# and it is one of the modes that actually works on a network drive
assert cr.sandbox_warning(r"\\server\share\repo", store.DEFAULT_SANDBOX) is None
print("default sandbox is full access and network-drive safe OK")

# ---- default_cwd resolves without hard-coding a machine path into behaviour ----
saved = os.environ.pop("TREE_AGENT_CWD", None)
try:
    baseline = store.default_cwd()
    assert os.path.isdir(baseline), baseline

    os.environ["TREE_AGENT_CWD"] = os.path.dirname(os.path.abspath(__file__))
    assert store.default_cwd() == os.path.dirname(os.path.abspath(__file__))

    # an override pointing nowhere is ignored rather than handed to Codex
    os.environ["TREE_AGENT_CWD"] = os.path.join(tempfile.gettempdir(), "no-such-dir-xyz")
    assert store.default_cwd() == baseline, store.default_cwd()
    del os.environ["TREE_AGENT_CWD"]

    # with no candidate present at all it still returns a real directory
    real_candidates = store.CWD_CANDIDATES
    store.CWD_CANDIDATES = (os.path.join(tempfile.gettempdir(), "also-missing-xyz"),)
    try:
        assert store.default_cwd() == os.path.expanduser("~")
        assert os.path.isdir(store.default_cwd())
    finally:
        store.CWD_CANDIDATES = real_candidates
finally:
    if saved is not None:
        os.environ["TREE_AGENT_CWD"] = saved
print("default_cwd honours the override, ignores bad paths, always returns a real dir OK")

# ---- a fresh workspace picks both up ----
fresh = store.Workspace(tempfile.mkdtemp())
assert fresh.defaults["sandbox"] == store.DEFAULT_SANDBOX, fresh.defaults
assert fresh.defaults["cwd"] == store.default_cwd(), fresh.defaults
assert fresh.defaults["model"] is None
print("fresh workspace defaults:", fresh.defaults)

# ---- a project that overrides nothing inherits them all the way down ----
top = fresh.projects[0]
sub = fresh.add_project(top["id"], "子專案")
deep = fresh.add_project(sub["id"], "更深的子專案")
conv = fresh.add_conversation(deep["id"], "對話")
effective = fresh.resolve(conv["id"])
assert effective["cwd"] == store.default_cwd(), effective
assert effective["sandbox"] == store.DEFAULT_SANDBOX, effective
# and that is what reaches the CLI
cmd = cr.Turn("p", effective["cwd"], lambda e: None,
              sandbox=effective["sandbox"]).build_command()
assert "--dangerously-bypass-approvals-and-sandbox" in cmd, cmd
assert "-s" not in cmd, cmd
print("nested project inherits the defaults down to the CLI flags OK")

# an explicit override still wins over the default
fresh.set_option(sub["id"], "sandbox", "read-only")
assert fresh.resolve(conv["id"])["sandbox"] == "read-only"
fresh.set_option(sub["id"], "sandbox", "")
assert fresh.resolve(conv["id"])["sandbox"] == store.DEFAULT_SANDBOX
print("per-project override still takes precedence OK")

# ---- the UI shows the new inherited values, and no warning ----
root = tk.Tk()
app = TreeAgentApp(root, home=tempfile.mkdtemp(), single_instance=False)
project = app.ws.projects[0]
app.refresh_tree(); app._select(project["id"]); root.update()
view = app.proj_view
assert view.sandbox_var.get() == INHERIT, view.sandbox_var.get()
assert store.default_cwd() in view.cwd_hint.cget("text"), view.cwd_hint.cget("text")
assert store.DEFAULT_SANDBOX in view.sandbox_hint.cget("text"), view.sandbox_hint.cget("text")
assert not view.warn_label.winfo_ismapped(), "the default combination must not warn"
print("project page shows the inherited defaults with no warning OK")

# the defaults dialog opens preselected on the shipped values
from tree_agent.app import DefaultsDialog
dialog = DefaultsDialog(root, app)
root.update()
assert dialog.cwd_var.get() == store.default_cwd(), dialog.cwd_var.get()
assert dialog.sandbox_var.get() == cr.sandbox_label(store.DEFAULT_SANDBOX), dialog.sandbox_var.get()
dialog.destroy()
print("defaults dialog preselects the shipped values OK")

app.on_close()
print("\nALL DEFAULTS TESTS PASSED")
