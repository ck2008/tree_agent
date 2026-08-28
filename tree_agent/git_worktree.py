"""Small, shell-free helpers for reviewing a local Git working tree."""

from __future__ import annotations

from dataclasses import dataclass
import subprocess
from pathlib import Path


class GitError(RuntimeError):
    pass


@dataclass(frozen=True)
class GitChange:
    path: str
    index_status: str
    worktree_status: str

    @property
    def untracked(self) -> bool:
        return self.index_status == "?" and self.worktree_status == "?"


def _run(repo: str, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True,
        encoding="utf-8", errors="replace", shell=False,
    )
    if check and result.returncode:
        detail = (result.stderr or result.stdout).strip() or f"git 結束代碼 {result.returncode}"
        raise GitError(detail)
    return result


def repository_root(cwd: str) -> str | None:
    if not cwd or not Path(cwd).is_dir():
        return None
    result = _run(cwd, "rev-parse", "--show-toplevel", check=False)
    return result.stdout.strip() if result.returncode == 0 else None


def changes(repo: str) -> list[GitChange]:
    root = repository_root(repo)
    if root is None:
        raise GitError("這不是 Git 儲存庫")
    raw = _run(root, "status", "--porcelain=v1", "-z").stdout
    result: list[GitChange] = []
    for record in raw.split("\0"):
        if not record:
            continue
        # Rename entries include a second NUL-delimited source name. The first
        # path is enough for display and Git accepts it for restore.
        if len(record) < 4:
            continue
        result.append(GitChange(record[3:], record[0], record[1]))
    return result


def diff(repo: str, path: str) -> str:
    root = repository_root(repo)
    if root is None:
        raise GitError("這不是 Git 儲存庫")
    unstaged = _run(root, "diff", "--no-ext-diff", "--binary", "--", path).stdout
    staged = _run(root, "diff", "--cached", "--no-ext-diff", "--binary", "--", path).stdout
    return staged + ("\n" if staged and unstaged else "") + unstaged


def has_head(repo: str) -> bool:
    root = repository_root(repo)
    return bool(root and _run(root, "rev-parse", "--verify", "HEAD", check=False).returncode == 0)


def restore_tracked(repo: str, paths: list[str]) -> None:
    root = repository_root(repo)
    if root is None:
        raise GitError("這不是 Git 儲存庫")
    if not has_head(root):
        raise GitError("此儲存庫尚無 HEAD，無法還原")
    if paths:
        _run(root, "restore", "--source=HEAD", "--staged", "--worktree", "--", *paths)


def remove_untracked(repo: str, paths: list[str]) -> None:
    root = repository_root(repo)
    if root is None:
        raise GitError("這不是 Git 儲存庫")
    if paths:
        _run(root, "clean", "-f", "--", *paths)


def commit_all(repo: str, message: str) -> None:
    root = repository_root(repo)
    if root is None:
        raise GitError("這不是 Git 儲存庫")
    message = message.strip()
    if not message:
        raise GitError("請輸入 commit 訊息")
    _run(root, "add", "-A")
    _run(root, "commit", "-m", message)
