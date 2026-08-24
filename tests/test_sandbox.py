"""Network-drive sandbox handling, and the benign-notice downgrade.

Background: the *sandboxed* modes launch commands as a separate restricted user
via CreateProcessWithLogonW. Drive-letter mappings are per logon session, so
`F:\\...` does not resolve for that user and every command fails with
ERROR_DIRECTORY (267). The full-access modes build no sandbox, run as you, and
work fine. Verified by hand against codex 0.147 and re-confirmed on 0.149, on \\\\192.168.1.146\\d$:

    E:\\... (local)   -s read-only            -> works
    F:\\... (network) -s read-only            -> CreateProcessWithLogonW 267
    F:\\... (network) -s workspace-write      -> CreateProcessWithLogonW 267
    F:\\... (network) -s danger-full-access   -> works, wrote the file (exit 0)
    F:\\... (network) bypass sandbox          -> works (exit 0)
"""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import ctypes
import tempfile
import tkinter as tk
from tree_agent import codex_runner as cr
from tree_agent.app import TreeAgentApp

# ---- the no-sandbox mode maps to the bypass flag, not to -s ----
assert cr.NO_SANDBOX in cr.SANDBOX_MODES
cmd = cr.Turn("p", ".", lambda e: None, sandbox=cr.NO_SANDBOX).build_command()
assert "--dangerously-bypass-approvals-and-sandbox" in cmd, cmd
assert "-s" not in cmd, cmd
cmd = cr.Turn("p", ".", lambda e: None, sandbox="read-only").build_command()
assert cmd[cmd.index("-s") + 1] == "read-only"
assert "--dangerously-bypass-approvals-and-sandbox" not in cmd
# the bypass flag must still precede the resume sub-command
cmd = cr.Turn("p", ".", lambda e: None, sandbox=cr.NO_SANDBOX, thread_id="tid").build_command()
assert cmd.index("--dangerously-bypass-approvals-and-sandbox") < cmd.index("resume"), cmd
print("sandbox mode -> CLI flag mapping OK")

# ---- network path detection ----
assert cr.is_network_path(r"\\server\share\dir") is True
assert cr.is_network_path("//server/share") is True
assert cr.is_network_path(None) is False
assert cr.is_network_path("") is False

# find a real local and (if present) a real network drive on this machine
def drive_type(letter):
    return ctypes.windll.kernel32.GetDriveTypeW(f"{letter}:\\")

local = next((c for c in "CDE" if drive_type(c) == 3), None)
network = next((c for c in "FGHIJKLMNOPQRSTUVWXYZ" if drive_type(c) == 4), None)
assert local, "expected at least one local fixed drive"
assert cr.is_network_path(f"{local}:\\some\\dir") is False, local
print(f"local drive {local}: detected as local OK")
if network:
    assert cr.is_network_path(f"{network}:\\some\\dir") is True, network
    print(f"network drive {network}: detected as network OK")
else:
    print("no mapped network drive on this machine; UNC cases still covered")

# ---- the warning fires exactly when the combination would fail ----
netpath = r"\\192.168.1.146\d$\eic_server\eic_server_std_dev\kws"
localpath = f"{local}:\\repo"
assert cr.sandbox_warning(netpath, "read-only")
assert cr.sandbox_warning(netpath, "workspace-write")
# Measured, not assumed: danger-full-access builds no sandbox, so it works on a
# network drive and must NOT be warned about.
assert cr.sandbox_warning(netpath, "danger-full-access") is None
assert cr.sandbox_warning(netpath, cr.NO_SANDBOX) is None
assert cr.sandbox_warning(localpath, "read-only") is None
assert cr.sandbox_warning(localpath, "workspace-write") is None
assert cr.sandbox_warning(None, "read-only") is None
warning = cr.sandbox_warning(netpath, "read-only")
assert "267" in warning
assert "danger-full-access" in warning, "the warning must name the mode that works"
print("sandbox_warning fires only on the two sandboxed modes OK")

# ---- combobox labels round-trip to the stored mode names ----
for mode in cr.SANDBOX_MODES:
    label = cr.sandbox_label(mode)
    assert label and label != mode, f"{mode} needs a readable label"
    assert cr.sandbox_from_label(label) == mode, (mode, label)
    assert cr.sandbox_from_label(mode) == mode, "a bare mode name passes through"
assert "最高權限" in cr.sandbox_label("danger-full-access")
assert len(set(cr.SANDBOX_LABELS.values())) == len(cr.SANDBOX_MODES), "labels must be unique"
print("sandbox labels round-trip OK")

# ---- benign Codex notices are not shown as errors ----
role, text = cr.describe_item({
    "type": "error",
    "message": "Skill descriptions were shortened to fit the skills context budget. "
               "Codex can still see every skill, but some descriptions are shorter.",
})
assert role == "notice", role
assert "shortened" in text, "the notice text is kept verbatim, just not in red"
role, _ = cr.describe_item({"type": "error", "message": "stream disconnected before completion"})
assert role == "error", "a real failure stays an error"
print("benign notice downgraded, real errors untouched OK")

# ---- item.started / item.updated must not leak raw JSON into the transcript ----
seen = []
turn = cr.Turn("p", ".", seen.append)
turn._handle_stdout_line('{"type":"item.started","item":{"type":"command_execution","command":"dir"}}')
turn._handle_stdout_line('{"type":"item.updated","item":{"type":"command_execution","command":"dir"}}')
assert seen == [], seen
turn._handle_stdout_line(
    '{"type":"item.completed","item":{"type":"command_execution","command":"dir",'
    '"exit_code":-1,"aggregated_output":"execution error: CreateProcessWithLogonW failed: 267"}}'
)
assert len(seen) == 1 and seen[0]["role"] == "tool", seen
assert "267" in seen[0]["text"]
print("progress echoes suppressed, completed item rendered OK")

# ---- the UI surfaces the warning before you press send ----
home = tempfile.mkdtemp()
root = tk.Tk()
app = TreeAgentApp(root, home=home, single_instance=False)
top = app.ws.projects[0]
conv = top["children"][0]
app.ws.set_option(top["id"], "cwd", netpath)
app.ws.set_option(top["id"], "sandbox", "workspace-write")
app.refresh_tree()
app._select(conv["id"])
root.update()
assert app.conv_view.warn_label.winfo_ismapped(), "conversation header should warn"
assert "267" in app.conv_view.warn_label.cget("text")

app._select(top["id"])
root.update()
assert app.proj_view.warn_label.winfo_ismapped(), "project settings should warn"

# switching to no-sandbox clears both warnings
app.ws.set_option(top["id"], "sandbox", cr.NO_SANDBOX)
app.proj_view.show(app.ws.find(top["id"]))
root.update()
assert not app.proj_view.warn_label.winfo_ismapped()
app._select(conv["id"])
root.update()
assert not app.conv_view.warn_label.winfo_ismapped()
print("UI warns on the bad combination and clears it on the fix OK")

# ---- the settings form shows labels but stores bare mode names ----
app._select(top["id"])
root.update()
form_value = app.proj_view.sandbox_var.get()
assert form_value == cr.sandbox_label(cr.NO_SANDBOX), form_value

app.proj_view.sandbox_var.set(cr.sandbox_label("danger-full-access"))
app.proj_view.save()
root.update()
stored = app.ws.find(top["id"])["sandbox"]
assert stored == "danger-full-access", stored
assert app.ws.resolve(conv["id"])["sandbox"] == "danger-full-access"
# and that stored value is what reaches the CLI
cmd = cr.Turn("p", netpath, lambda e: None, sandbox=stored).build_command()
assert cmd[cmd.index("-s") + 1] == "danger-full-access", cmd
print("settings form stores bare mode names, not labels OK")

# choosing "(inherit)" clears the override rather than storing the literal
app.proj_view.sandbox_var.set("(繼承)")
app.proj_view.save()
assert app.ws.find(top["id"])["sandbox"] is None, app.ws.find(top["id"])["sandbox"]
print("inherit clears the override OK")

app.on_close()
print("\nALL SANDBOX TESTS PASSED")
