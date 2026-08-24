"""One workspace folder may only be opened by one window at a time."""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import subprocess, tempfile, tkinter as tk
from tree_agent import store
from tree_agent.app import TreeAgentApp

home = tempfile.mkdtemp()
other = tempfile.mkdtemp()

# ---- the lock is exclusive per folder ----
first = store.WorkspaceLock(home)
assert first.acquire() is True
assert first.holder_pid() == str(os.getpid()), first.holder_pid()

second = store.WorkspaceLock(home)
assert second.acquire() is False, "a second lock on the same folder must fail"

# a different --home is unaffected
elsewhere = store.WorkspaceLock(other)
assert elsewhere.acquire() is True
elsewhere.release()
print("lock is exclusive per folder, independent across folders OK")

# ---- releasing hands it over ----
first.release()
assert second.acquire() is True, "lock was not released"
second.release()
print("release hands the lock over OK")

# ---- a dead process leaves no stale lock (the OS drops it) ----
child = subprocess.run(
    [sys.executable, "-c",
     "import sys; sys.path.insert(0, r'%s');\n"
     "from tree_agent import store\n"
     "lk = store.WorkspaceLock(r'%s')\n"
     "print(lk.acquire())" % (os.path.dirname(os.path.dirname(os.path.abspath(__file__))), home)],
    capture_output=True, text=True,
)
assert child.stdout.strip() == "True", (child.stdout, child.stderr)
after = store.WorkspaceLock(home)
assert after.acquire() is True, "lock survived the process that held it"
after.release()
print("no stale lock after the holder exits OK")

# ---- the app takes the lock, and frees it on close ----
root = tk.Tk()
app = TreeAgentApp(root, home=home)
root.update()
assert app.lock is not None
blocked = store.WorkspaceLock(home)
assert blocked.acquire() is False, "the running app should hold the lock"
app.on_close()
assert blocked.acquire() is True, "closing the app should release the lock"
blocked.release()
print("app acquires on open and releases on close OK")

# ---- single_instance=False opts out (used by the other test suites) ----
root2 = tk.Tk()
app2 = TreeAgentApp(root2, home=home, single_instance=False)
root2.update()
assert app2.lock is None
free = store.WorkspaceLock(home)
assert free.acquire() is True, "opting out must not take the lock"
free.release()
app2.on_close()
print("single_instance=False opts out OK")

print("\nALL LOCK TESTS PASSED")
