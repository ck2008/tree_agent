"""Non-secret desktop preferences survive restarts and tolerate bad files."""

from __future__ import annotations

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tree_agent.preferences import DesktopPreferences
from tree_agent.app import _ensure_email_verified


class _UnverifiedClient:
    def me(self) -> dict[str, object]:
        return {"email_verified": False}


class _ParentThatMustNotWait:
    def wait_window(self, _dialog: object) -> None:
        raise AssertionError("suppressed verification must not open a dialog")


with tempfile.TemporaryDirectory(prefix="tree-agent-preferences-") as home:
    preferences = DesktopPreferences(home)
    assert not preferences.email_verification_prompt_suppressed()

    preferences.set_email_verification_prompt_suppressed(True)
    assert preferences.email_verification_prompt_suppressed()
    assert "suppress_email_verification_prompt" in preferences.path.read_text(encoding="utf-8")
    assert not _ensure_email_verified(_ParentThatMustNotWait(), _UnverifiedClient(), preferences)  # type: ignore[arg-type]

    preferences.set_email_verification_prompt_suppressed(False)
    assert not preferences.email_verification_prompt_suppressed()

    preferences.path.write_text("not json", encoding="utf-8")
    assert not preferences.email_verification_prompt_suppressed()

print("test_preferences OK")
