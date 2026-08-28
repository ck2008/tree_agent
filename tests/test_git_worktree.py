"""Git review helpers stay inside the selected repository and avoid a shell."""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tree_agent import git_worktree


def git(repo: str, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)


with tempfile.TemporaryDirectory(prefix="tree-agent-git-") as home:
    git(home, "init")
    git(home, "config", "user.email", "test@example.invalid")
    git(home, "config", "user.name", "Tree Agent Test")
    tracked = Path(home, "tracked.txt")
    tracked.write_text("first\n", encoding="utf-8")
    git(home, "add", "tracked.txt")
    git(home, "commit", "-m", "initial")

    tracked.write_text("changed\n", encoding="utf-8")
    extra = Path(home, "extra.txt")
    extra.write_text("new\n", encoding="utf-8")
    assert os.path.normcase(os.path.normpath(git_worktree.repository_root(home) or "")) == os.path.normcase(os.path.normpath(home))
    snapshot = {change.path: change for change in git_worktree.changes(home)}
    assert "tracked.txt" in snapshot and "extra.txt" in snapshot
    assert snapshot["extra.txt"].untracked
    assert "-first" in git_worktree.diff(home, "tracked.txt")

    git_worktree.restore_tracked(home, ["tracked.txt"])
    assert tracked.read_text(encoding="utf-8") == "first\n"
    git_worktree.remove_untracked(home, ["extra.txt"])
    assert not extra.exists()

    tracked.write_text("for commit\n", encoding="utf-8")
    git_worktree.commit_all(home, "save change")
    assert not git_worktree.changes(home)

with tempfile.TemporaryDirectory(prefix="tree-agent-not-git-") as plain:
    assert git_worktree.repository_root(plain) is None
    try:
        git_worktree.changes(plain)
    except git_worktree.GitError:
        pass
    else:
        raise AssertionError("non-git folder must be rejected")

print("test_git_worktree OK")
