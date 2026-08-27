"""Run every test module in this folder and report a summary.

`test_gui`, `test_extra` and `test_resilience` open real Tk windows and drive
real `codex exec` turns, so they need a desktop session, a working `codex` on
PATH, and a few minutes.

The `test_server_*` suites cover the shared SQLite service. They are offline and
need no desktop, but `test_server_client` starts a real uvicorn on a loopback
port and moves 20 MiB through it, so it is the slowest of them.

    python tests/run_all.py            # everything
    python tests/run_all.py core       # only the offline suite
"""

from __future__ import annotations

import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
SUITES = [
    "test_core",
    "test_server_ids",
    "test_server_db",
    "test_server_core",
    "test_server_auth",
    "test_server_email_auth",
    "test_server_mail_settings",
    "test_server_search",
    "test_server_attachments",
    "test_server_retention",
    "test_server_migration",
    "test_server_client",
    "test_server_desktop",
    "test_session_store",
    "test_richtext",
    "test_user_logs",
    "test_defaults",
    "test_prompt",
    "test_create",
    "test_autoname",
    "test_tree_state",
    "test_lock",
    "test_sandbox",
    "test_ansi",
    "test_copy",
    "test_fork",
    "test_features",
    "test_paste",
    "test_attachment_view",
    "test_tool_display",
    "test_theme",
    "test_agents",
    "test_reading",
    "test_panels",
    "test_transfer",
    "test_stale_events",
    "test_drafts",
    "test_layout",
    "test_gui",
    "test_extra",
    "test_resilience",
]


def main(argv: list[str]) -> int:
    wanted = argv or SUITES
    suites = [s if s.startswith("test_") else f"test_{s}" for s in wanted]

    env = dict(os.environ, PYTHONUTF8="1", PYTHONIOENCODING="utf-8")
    failures = []
    for suite in suites:
        print(f"\n{'=' * 70}\n{suite}\n{'=' * 70}")
        started = time.monotonic()
        result = subprocess.run(
            [sys.executable, os.path.join(HERE, suite + ".py")], env=env
        )
        elapsed = time.monotonic() - started
        status = "PASS" if result.returncode == 0 else "FAIL"
        print(f"--- {suite}: {status} ({elapsed:.1f}s)")
        if result.returncode != 0:
            failures.append(suite)

    print(f"\n{'=' * 70}")
    if failures:
        print("FAILED:", ", ".join(failures))
        return 1
    print(f"All {len(suites)} suites passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
