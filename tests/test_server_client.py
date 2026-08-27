"""End to end over real HTTP, driven by the desktop client library.

Everything here goes through uvicorn and `tree_agent.client_api`, so it covers
the parts the in-process tests cannot: status codes, cookies and bearer tokens,
streamed chunk uploads and downloads, and what happens when a client retries a
request whose response it never saw.
"""

from __future__ import annotations

import hashlib
import os
import tempfile
import threading

from server_support import ADMIN_PASSWORD, BOOTSTRAP_TOKEN, banner, make_services, serve

from tree_agent.client_api import ApiError, WorkspaceClient, new_key
from tree_agent.server.services.attachments import MAX_BYTES

services = make_services()
home = services.home


def expect_status(status: int, fn, *args, **kwargs):
    try:
        fn(*args, **kwargs)
    except ApiError as exc:
        assert exc.status == status, f"expected {status}, got {exc.status}: {exc.detail}"
        return exc
    raise AssertionError(f"expected HTTP {status}, got success")


with serve(services) as base_url:
    print("serving on", base_url)
    client = WorkspaceClient(base_url)

    banner("health and bootstrap")
    assert client.health()["needs_bootstrap"] is True
    client.bootstrap(
        token=BOOTSTRAP_TOKEN, username="admin", password=ADMIN_PASSWORD,
        email="admin@example.test",
    )
    assert client.health()["needs_bootstrap"] is False

    banner("an unauthenticated client gets 401, not data")
    expect_status(401, client.tree)
    client.login("admin", ADMIN_PASSWORD)
    assert client.me()["role"] == "admin"

    banner("administrator mail settings round-trip without exposing a password")
    settings = client.get_mail_settings()
    assert settings["has_password"] is False
    saved_settings = client.update_mail_settings(
        host="smtp.example.test", port=2525, from_address="noreply@example.test",
        security="starttls", username="",
    )
    assert saved_settings["host"] == "smtp.example.test"
    assert saved_settings["encryption"] == "starttls"

    banner("the tree round-trips over HTTP")
    project = client.create_project(parent_id=None, name="遠端專案")
    conversation = client.create_conversation(project_id=project["id"], name="遠端對話")
    assert client.path_of(conversation["id"]) == "遠端專案 / 遠端對話"
    assert [p["name"] for p in client.tree()["projects"]] == ["遠端專案"]

    banner("a retry with the same Idempotency-Key creates one message, not two")
    key = new_key()
    first = client.append_message(
        conversation["id"], role="user", content="只該出現一次", idempotency_key=key
    )
    replay = client.append_message(
        conversation["id"], role="user", content="只該出現一次", idempotency_key=key
    )
    assert first["id"] == replay["id"], (first["id"], replay["id"])
    transcript = list(client.iter_messages(conversation["id"]))
    assert [m["content"] for m in transcript] == ["只該出現一次"], transcript
    print("two identical requests, one message:", first["id"][:8])

    banner("reusing a key for a different request is refused")
    error = expect_status(
        409,
        client.append_message,
        conversation["id"],
        role="user",
        content="不一樣的內容",
        idempotency_key=key,
    )
    assert error.code == "idempotency_key_reused", error.code
    assert len(list(client.iter_messages(conversation["id"]))) == 1

    banner("without a key, a genuine resend does duplicate — which is why keys exist")
    client.append_message(conversation["id"], role="user", content="重複的內容")
    client.append_message(conversation["id"], role="user", content="重複的內容")
    assert len(list(client.iter_messages(conversation["id"]))) == 3

    banner("runner events dedupe on their own external id")
    for _ in range(3):
        event = client.append_message(
            conversation["id"], role="agent", content="串流的回答", external_event_id="evt-1"
        )
    assert sum(1 for m in client.iter_messages(conversation["id"]) if m["content"] == "串流的回答") == 1
    print("the same runner event delivered three times stored once:", event["id"][:8])

    banner("streaming a reply in deltas")
    streamed = client.append_message(conversation["id"], role="agent", content="")
    for piece in ("先讀 store.py，", "再改 API，", "最後換掉 UI。"):
        client.append_delta(streamed["id"], piece)
    client.complete_message(
        streamed["id"], usage={"input_tokens": 1200, "output_tokens": 340}
    )
    final = [m for m in client.iter_messages(conversation["id"]) if m["id"] == streamed["id"]][0]
    print("streamed:", final["content"])
    assert final["content"] == "先讀 store.py，再改 API，最後換掉 UI。"
    assert final["completed_at"] is not None
    assert client.usage_of(conversation["id"])["input_tokens"] == 1200

    banner("tool calls are recorded against their message")
    call = client.add_tool_call(
        streamed["id"], tool_name="shell", payload={"command": "dir"}, status="running"
    )
    client.update_tool_call(call["id"], status="completed", output_text="Volume in drive E")
    stored = [m for m in client.iter_messages(conversation["id"]) if m["id"] == streamed["id"]][0]
    assert stored["tool_calls"][0]["status"] == "completed"
    assert stored["tool_calls"][0]["input"] == {"command": "dir"}

    banner("a stale revision loses predictably")
    stale = client.project(project["id"])["revision"]
    client.update_project(project["id"], revision=stale, name="改名成功")
    error = expect_status(409, client.update_project, project["id"], revision=stale, name="改名失敗")
    print("conflict says the current revision is", error.current_revision)
    assert error.current_revision == stale + 1
    assert client.project(project["id"])["name"] == "改名成功"

    banner("every update can use an Idempotency-Key, and deletes require a revision")
    update_key = new_key()
    payload = {"revision": stale + 1, "fields": {"prompt": "只更新一次"}}
    first_update = client._request(
        "PATCH", f"/api/projects/{project['id']}", body=payload, idempotency_key=update_key
    )
    replay_update = client._request(
        "PATCH", f"/api/projects/{project['id']}", body=payload, idempotency_key=update_key
    )
    assert first_update == replay_update
    assert client.project(project["id"])["revision"] == stale + 2
    disposable = client.create_project(parent_id=None, name="必須版本的刪除")
    expect_status(422, client._request, "DELETE", f"/api/projects/{disposable['id']}")
    client._request(
        "DELETE",
        f"/api/projects/{disposable['id']}",
        params={"revision": disposable["revision"]},
        idempotency_key=new_key(),
    )

    banner("two clients racing to reorder the same level both keep their data")
    holder = client.create_project(parent_id=None, name="競賽")
    made = [client.create_conversation(project_id=holder["id"], name=f"C{i}") for i in range(8)]
    errors: list[Exception] = []

    def shuffle(offset: int) -> None:
        racer = WorkspaceClient(base_url, token=client.token)
        for round_number in range(6):
            target = made[(offset + round_number) % len(made)]
            try:
                current = racer.conversation(target["id"])
                racer.move_conversation(
                    target["id"],
                    revision=current["revision"],
                    project_id=holder["id"],
                    index=(offset + round_number) % len(made),
                )
            except ApiError as exc:
                if not exc.is_conflict:
                    errors.append(exc)

    threads = [threading.Thread(target=shuffle, args=(offset,)) for offset in (0, 3, 5)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert not errors, errors
    names = [c["name"] for c in client.conversations(holder["id"])]
    print("after three racing reorderers:", names)
    assert sorted(names) == sorted(c["name"] for c in made), names

    banner("a 20 MiB attachment over HTTP, chunk by chunk")
    source = os.path.join(home, "big-upload.bin")
    block = hashlib.sha256(b"seed").digest()
    with open(source, "wb") as handle:
        written = 0
        while written < MAX_BYTES:
            block = hashlib.sha256(block).digest()
            handle.write(block)
            written += len(block)
        handle.truncate(MAX_BYTES)
    expected_hash = hashlib.sha256(open(source, "rb").read()).hexdigest()

    carrier = client.append_message(conversation["id"], role="user", content="這是大檔案")
    uploaded = client.upload_file(
        conversation_id=conversation["id"], path=source, message_id=carrier["id"]
    )
    print("uploaded", uploaded["byte_size"], "bytes as", uploaded["chunk_count"], "chunks")
    assert uploaded["sha256"] == expected_hash

    downloaded = client.download_attachment(uploaded["id"], os.path.join(home, "roundtrip.bin"))
    assert hashlib.sha256(open(downloaded, "rb").read()).hexdigest() == expected_hash
    assert os.path.getsize(downloaded) == MAX_BYTES
    print("downloaded and verified against the server's own hash")

    banner("a viewer cannot reach an attachment in a project they were not given")
    client.create_user(
        username="alice", password="alice-password", email="alice@example.test", role="member"
    )
    alice = WorkspaceClient(base_url)
    alice.login("alice", "alice-password")
    expect_status(404, alice.attachment, uploaded["id"])
    expect_status(404, alice.project, project["id"])
    assert alice.tree()["projects"] == []
    users = {u["username"]: u for u in client.users()}
    client.grant(project["id"], user_id=users["alice"]["id"], permission="viewer")
    assert alice.attachment(uploaded["id"])["sha256"] == expected_hash
    expect_status(403, alice.append_message, conversation["id"], role="user", content="不該成功")
    print("alice can read once granted, and still cannot write")

    banner("search over HTTP")
    found = client.search("大檔案")["results"]
    assert [r["kind"] for r in found] == ["message"], found
    # The project was renamed by the revision test above.
    assert found[0]["path"] == "改名成功 / 遠端對話", found[0]["path"]
    assert alice.search("大檔案")["results"], "the granted viewer should find it too"

    banner("logout ends the session")
    alice.logout()
    alice.token = None
    expect_status(401, alice.tree)

    banner("backup, then verify the copy in isolation")
    backup_path = os.path.join(home, "backups", "snapshot.db")
    report = client.backup(backup_path)
    print("backup:", os.path.basename(report["path"]), "integrity:", report["integrity"])
    assert report["integrity"] == "ok"

restored_home = tempfile.mkdtemp(prefix="restored-")
restored_db = os.path.join(restored_home, "tree-agent.db")
os.replace(backup_path, restored_db)
restored = make_services(restored_home)
assert restored.db.integrity_check() == "ok"

banner("the restored copy can be logged into, searched and read")
with serve(restored) as restored_url:
    verifier = WorkspaceClient(restored_url)
    verifier.login("admin", ADMIN_PASSWORD)
    assert [p["name"] for p in verifier.tree()["projects"]] == ["改名成功", "競賽"]
    assert verifier.search("大檔案")["results"]
    copy = verifier.download_attachment(uploaded["id"], os.path.join(restored_home, "copy.bin"))
    assert hashlib.sha256(open(copy, "rb").read()).hexdigest() == expected_hash
    print("logged in, searched and downloaded a 20 MiB attachment from the restored backup")

restored.close()
services.close()
print("\ntest_server_client OK")
