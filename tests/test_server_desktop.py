"""Desktop-to-service adapter tests; no Tk or server process is needed."""

from __future__ import annotations

import tempfile
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tree_agent import store
from tree_agent.client_api import RemoteWorkspace


class FakeClient:
    def __init__(self):
        self.calls = []
        self.base_url = "https://shared.example"
        self.project = {
            "id": "project-1", "name": "Shared", "revision": 1, "expanded": True,
            "children": [], "conversations": [{
                "id": "conversation-1", "project_id": "project-1", "name": "Chat",
                "revision": 1, "agent_id": "codex", "codex_thread_id": None,
                "claude_session_id": None, "forked_from_external_session_id": None,
            }],
        }
        self.messages_by_conversation = {"conversation-1": []}
        self.counter = 0

    def tree(self):
        return {"defaults": {"cwd": "C:\\work", "sandbox": "read-only"}, "projects": [self.project]}

    def iter_messages(self, conversation_id):
        return iter(self.messages_by_conversation[conversation_id])

    def path_of(self, node_id):
        return "Shared / Chat" if node_id == "conversation-1" else "Shared"

    def settings_for(self, node_id):
        return {"cwd": "C:\\work", "sandbox": "read-only", "model": None}

    def instructions_for(self, node_id):
        return ""

    def usage_of(self, node_id):
        return {"turns": 1, "input_tokens": 10, "output_tokens": 3} if self.calls else {}

    def append_message(self, conversation_id, **kwargs):
        self.counter += 1
        message = {"id": f"message-{self.counter}", "content": kwargs["content"],
                   "role": kwargs["role"], "created_at": "now", "attachments": []}
        self.messages_by_conversation[conversation_id].append(message)
        self.calls.append(("append", conversation_id, kwargs))
        return message

    def complete_message(self, message_id, **kwargs):
        self.calls.append(("complete", message_id, kwargs))
        return {"id": message_id}

    def set_runner_state(self, conversation_id, **kwargs):
        self.calls.append(("runner", conversation_id, kwargs))
        return {"id": conversation_id, "codex_thread_id": kwargs.get("codex_thread_id"),
                "claude_session_id": kwargs.get("claude_session_id")}

    def update_project(self, project_id, **kwargs):
        self.calls.append(("project", project_id, kwargs))
        self.project.update(kwargs)
        self.project["revision"] += 1
        # Project PATCH replies are a project row, not a tree with cached
        # children/conversations. Returning only updated fields models that
        # boundary and prevents the adapter cache from being overwritten.
        return {"id": project_id, "revision": self.project["revision"], **kwargs}

    def update_conversation(self, conversation_id, **kwargs):
        self.calls.append(("conversation", conversation_id, kwargs))
        conversation = self.project["conversations"][0]
        conversation.update(kwargs)
        conversation["revision"] += 1
        return dict(conversation)


client = FakeClient()
home = tempfile.mkdtemp(prefix="tree-agent-desktop-")
workspace = RemoteWorkspace(client, home)
conversation = workspace.find("conversation-1")
assert conversation and conversation["kind"] == store.CONVERSATION
assert conversation["messages"] == []

# Runner state and all transcript writes go through the service, not a JSON workspace.
workspace.set_thread_id("conversation-1", "thread-1")
workspace.append_message("conversation-1", "user", "hello")
workspace.append_message("conversation-1", "agent", "world", agent_id="codex")
assert conversation["thread_id"] == "thread-1"
assert [message["text"] for message in conversation["messages"]] == ["hello", "world"]
workspace.add_usage("conversation-1", {"input_tokens": 10, "output_tokens": 3})
assert any(call[0] == "complete" and call[2]["usage"]["output_tokens"] == 3 for call in client.calls)

# Project setting changes retain revision checking at the API boundary.
workspace.set_option("project-1", "prompt", "shared rule")
project_call = next(call for call in client.calls if call[0] == "project")
assert project_call[2]["revision"] == 1 and project_call[2]["prompt"] == "shared rule"

# Local runners must not inherit another computer's shared path or runner.
calls_before = len(client.calls)
workspace.set_option("project-1", "cwd", r"D:\Local\Shared")
workspace.set_option("project-1", "default_agent", store.CLAUDE_AGENT)
workspace.set_conversation_agent("conversation-1", None)
assert workspace.resolve("conversation-1")["cwd"] == r"D:\Local\Shared"
assert workspace.conversation_agent("conversation-1") == store.CLAUDE_AGENT, (
    workspace.find("project-1"), workspace._execution
)
assert workspace.conversation_agent_source("conversation-1") == "專案"
assert len(client.calls) == calls_before, "local runner settings must not touch shared API"
record_id = workspace.start_execution_record("conversation-1", {"status": "running"})
workspace.update_execution_record("conversation-1", record_id, status="completed")
assert workspace.execution_records("conversation-1")[-1]["status"] == "completed"

# Only UI and per-machine runner preferences are written locally.
workspace.data["ui"]["theme"] = "dark"
workspace.set_agent_path("codex", r"C:\\Tools\\codex.exe")
assert workspace.agent_path("codex") == r"C:\\Tools\\codex.exe"
assert not (workspace.home and __import__("os").path.exists(__import__("os").path.join(workspace.home, "workspace.json")))
restored = RemoteWorkspace(client, home)
assert restored.resolve("conversation-1")["cwd"] == r"D:\Local\Shared"
assert restored.conversation_agent("conversation-1") == store.CLAUDE_AGENT
print("desktop adapter: runner events, messages, usage and local preferences OK")
