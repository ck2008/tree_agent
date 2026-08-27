"""The desktop must remember only a Windows-protected opaque session token."""

from __future__ import annotations

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tree_agent.session_store import SessionStore


with tempfile.TemporaryDirectory(prefix="tree-agent-session-") as home:
    store = SessionStore(home)
    store.save("opaque-session-token")
    assert store.load() == "opaque-session-token"
    assert b"opaque-session-token" not in store.path.read_bytes()
    store.clear()
    assert store.load() is None

print("test_session_store OK")
