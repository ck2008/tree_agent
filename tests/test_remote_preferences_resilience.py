"""A local preference write failure must not hide a server-created conversation."""

from __future__ import annotations

import os
import sys
import tempfile
from unittest.mock import patch
import json

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tree_agent import store
from tree_agent.client_api import RemoteWorkspace


class FakeClient:
    base_url = "https://shared.example"

    def __init__(self) -> None:
        self.project = {
            "id": "project-1", "name": "Shared", "revision": 1, "expanded": True,
            "children": [], "conversations": [],
        }

    def tree(self):
        return {"defaults": {}, "projects": [self.project]}

    def iter_messages(self, _conversation_id):
        return iter(())

    def create_conversation(self, *, project_id, name, agent_id):
        created = {
            "id": "conversation-1", "project_id": project_id, "name": name,
            "revision": 1, "agent_id": agent_id, "codex_thread_id": None,
            "claude_session_id": None, "forked_from_external_session_id": None,
        }
        self.project["conversations"].append(created)
        return dict(created)


workspace = RemoteWorkspace(FakeClient(), tempfile.mkdtemp(prefix="tree-agent-pref-error-"))
with patch.object(workspace, "save", side_effect=PermissionError("access denied")):
    created = workspace.add_conversation("project-1", "新對話")

assert created["id"] == "conversation-1"
assert workspace.find("conversation-1")["kind"] == store.CONVERSATION
assert isinstance(workspace.last_local_save_error, PermissionError)
print("server-created conversation remains visible when desktop.json cannot be saved")

# A transient Windows sharing violation must not keep reporting a failed save.
resilient = RemoteWorkspace(FakeClient(), tempfile.mkdtemp(prefix="tree-agent-pref-retry-"))
resilient.data["ui"] = {"theme": "dark"}
with patch("tree_agent.client_api.os.replace", side_effect=PermissionError(5, "access denied")), \
     patch("tree_agent.client_api.time.sleep"):
    resilient.save()
with open(resilient._prefs_path, encoding="utf-8") as handle:
    assert json.load(handle)["ui"] == {"theme": "dark"}
print("desktop preferences fall back safely when rename is locked")
