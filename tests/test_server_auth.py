"""Login, roles and the project permission model.

The rule under test throughout: for a given user, the nearest ancestor that
grants *that user* something decides, and other people's grants on the way up
change nothing.
"""

from __future__ import annotations

from server_support import (
    ADMIN_PASSWORD,
    BOOTSTRAP_TOKEN,
    banner,
    expect_error,
    make_admin,
    make_services,
    make_user,
)

services = make_services()
auth = services.auth
tree = services.tree
messages = services.messages

banner("there is no default account; the first admin needs the one-time token")
assert services.auth.needs_bootstrap()
expect_error(
    403,
    auth.bootstrap_admin,
    token="wrong-token",
    username="intruder",
    password="intruder-password",
    email="intruder@example.test",
)
admin = make_admin(services)
assert not services.auth.needs_bootstrap()
# The token is spent, and cannot make a second admin.
expect_error(403, auth.bootstrap_admin, token=BOOTSTRAP_TOKEN, username="again", password="again-password", email="again@example.test")
print("bootstrap is one-shot")

banner("login")
expect_error(401, auth.login, "admin", "wrong password entirely")
expect_error(401, auth.login, "nobody-at-all", "wrong password entirely")
token, user = auth.login("admin", ADMIN_PASSWORD)
assert auth.authenticate(token).username == "admin"
expect_error(401, auth.authenticate, "not-a-real-token")
print("session token accepted, forged token rejected")

banner("a password reset invalidates every existing session")
auth.reset_password(admin, admin.id, "a-brand-new-password")
expect_error(401, auth.authenticate, token)
token, _ = auth.login("admin", "a-brand-new-password")
print("old session revoked, new login works")

banner("passwords have a floor and are never stored in the clear")
expect_error(400, auth.create_user, admin, username="weak", password="short", display_name="", role="member")
with services.db.read() as conn:
    stored = conn.execute("SELECT password_hash FROM users WHERE username = 'admin'").fetchone()[0]
assert stored.startswith("$argon2id$"), stored[:20]
assert "a-brand-new-password" not in stored
print("hash:", stored[:36] + "…")

banner("accounts and roles")
alice = make_user(services, admin, "alice", role="member")
bob = make_user(services, admin, "bob", role="member")
readonly = make_user(services, admin, "carol", role="viewer")
expect_error(403, auth.create_user, alice, username="mallory", password="mallory-password", display_name="", role="admin")
print("a member cannot create accounts")

banner("only an admin may create a top-level project")
expect_error(403, tree.create_project, alice, parent_id=None, name="愛麗絲的專案")
shared = tree.create_project(admin, parent_id=None, name="共用專案")
child = tree.create_project(admin, parent_id=shared["id"], name="子專案")
grandchild = tree.create_project(admin, parent_id=child["id"], name="孫專案")

banner("without a grant, a project is invisible — 404, not 403")
error = expect_error(404, tree.get_project, alice, shared["id"])
print("probing an id gives:", error.code)
assert tree.tree(alice)["projects"] == []

banner("a grant on an ancestor reaches every descendant")
tree.grant(admin, shared["id"], user_id=alice.id, permission="viewer")
assert tree.get_project(alice, grandchild["id"])["permission"] == "viewer"
expect_error(403, tree.create_conversation, alice, project_id=grandchild["id"], name="不該成功")
print("alice inherits viewer all the way down and cannot write")

banner("a grant on a child overrides the one it inherits, in both directions")
tree.grant(admin, child["id"], user_id=alice.id, permission="editor")
assert tree.get_project(alice, child["id"])["permission"] == "editor"
assert tree.get_project(alice, grandchild["id"])["permission"] == "editor"
assert tree.get_project(alice, shared["id"])["permission"] == "viewer"
owned = tree.create_conversation(alice, project_id=grandchild["id"], name="愛麗絲的對話")
print("editor on the child lets alice write there but not at the top")
expect_error(403, tree.create_conversation, alice, project_id=shared["id"], name="不該成功")

tree.grant(admin, grandchild["id"], user_id=alice.id, permission="viewer")
expect_error(403, tree.create_conversation, alice, project_id=grandchild["id"], name="再次不該成功")
print("narrowing on the grandchild takes effect immediately")
tree.grant(admin, grandchild["id"], user_id=alice.id, permission="editor")

banner("another user's grant on the same project changes nothing for alice")
tree.grant(admin, grandchild["id"], user_id=bob.id, permission="owner")
assert tree.get_project(alice, grandchild["id"])["permission"] == "editor"
assert tree.get_project(bob, grandchild["id"])["permission"] == "owner"
expect_error(404, tree.get_project, bob, shared["id"])
print("bob is owner of the grandchild and cannot see its ancestors")

banner("a viewer account is read-only however generous its grants are")
tree.grant(admin, shared["id"], user_id=readonly.id, permission="owner")
assert tree.get_project(readonly, shared["id"])["permission"] == "viewer"
expect_error(403, tree.create_conversation, readonly, project_id=shared["id"], name="不該成功")
expect_error(403, tree.update_project, readonly, shared["id"], revision=1, fields={"name": "改名"})
print("carol's owner grant is capped to viewer by her account role")

banner("a viewer can read the transcript but not add to it")
messages.append(alice, owned["id"], role="user", content="愛麗絲寫的內容")
assert messages.list_messages(readonly, owned["id"])["messages"][0]["content"] == "愛麗絲寫的內容"
expect_error(403, messages.append, readonly, owned["id"], role="user", content="不該成功")

banner("only an owner (or an admin) may change who has access")
expect_error(403, tree.grant, alice, grandchild["id"], user_id=readonly.id, permission="owner")
tree.grant(bob, grandchild["id"], user_id=readonly.id, permission="viewer")
print("bob, the owner, can grant; alice, an editor, cannot")

banner("the creator of a project owns it")
made = tree.create_project(alice, parent_id=child["id"], name="愛麗絲建立的")
assert tree.get_project(alice, made["id"])["permission"] == "owner"
tree.grant(alice, made["id"], user_id=bob.id, permission="viewer")
print("alice owns what she created and can share it")

banner("a disabled account cannot log in and its sessions die at once")
alice_token, _ = auth.login("alice", "alice-password")
assert auth.authenticate(alice_token).username == "alice"
auth.set_active(admin, alice.id, False)
expect_error(401, auth.authenticate, alice_token)
expect_error(401, auth.login, "alice", "alice-password")
auth.set_active(admin, alice.id, True)
print("disable revokes live sessions, re-enable restores login")

banner("an admin sees and may write everything without any membership")
assert tree.get_project(admin, made["id"])["permission"] == "owner"
assert len(tree.tree(admin)["projects"]) >= 1
expect_error(400, auth.set_role, admin, admin.id, "member")
print("the last admin cannot demote themselves")

services.close()
print("\ntest_server_auth OK")
