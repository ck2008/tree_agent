"""Email verification and password recovery stay local, opaque and one-time."""

from __future__ import annotations

import re

from server_support import ADMIN_PASSWORD, banner, expect_error, make_admin, make_services


outbox: list[tuple[str, str, str]] = []
services = make_services(mail_sender=lambda recipient, subject, text: outbox.append((recipient, subject, text)))
auth = services.auth
admin = make_admin(services)


def delivered_code() -> str:
    match = re.search(r"驗證碼：([0-9]{6})", outbox[-1][2])
    assert match, outbox[-1]
    return match.group(1)


banner("a bootstrap email exists but must be verified before recovery")
with services.db.read() as conn:
    row = conn.execute("SELECT email, email_verified_at_ms FROM users WHERE id = ?", (admin.id,)).fetchone()
assert tuple(row) == ("admin@example.test", None)
auth.request_password_reset("admin@example.test")
assert outbox == [], "unverified email must not receive recovery mail"

banner("email verification mails a one-time salted code, never stores it clear")
auth.request_email_verification(admin, "ADMIN@example.test")
code = delivered_code()
with services.db.read() as conn:
    record = conn.execute(
        "SELECT code_salt, code_hash, expires_at_ms, max_attempts FROM password_reset_codes"
    ).fetchone()
assert code not in record["code_hash"] and code not in record["code_salt"]
assert record["expires_at_ms"] > 0 and record["max_attempts"] == 5
verified = auth.confirm_email_verification(admin, email="admin@example.test", code=code)
assert verified["email"] == "admin@example.test" and verified["email_verified"] is True

banner("wrong verification codes stop after five attempts")
auth.request_email_verification(admin, "admin@example.test")
for _ in range(5):
    expect_error(400, auth.confirm_email_verification, admin, email="admin@example.test", code="000000")
expect_error(400, auth.confirm_email_verification, admin, email="admin@example.test", code=delivered_code())

banner("forgot-password is generic for an unknown email")
before = len(outbox)
auth.request_password_reset("nobody@example.test")
assert len(outbox) == before

banner("reset consumes its code and revokes every active session")
session, _ = auth.login("admin", ADMIN_PASSWORD)
auth.request_password_reset("admin@example.test")
reset_code = delivered_code()
auth.confirm_password_reset(
    email="admin@example.test", code=reset_code, password="new-verified-password"
)
expect_error(401, auth.authenticate, session)
expect_error(
    400,
    auth.confirm_password_reset,
    email="admin@example.test",
    code=reset_code,
    password="another-new-password",
)
assert auth.login("admin", "new-verified-password")[1]["email_verified"] is True

banner("HTTP API exposes generic recovery and authenticated verification routes")
client = __import__("server_support").make_http(services)
response = client.post("/api/auth/password-reset/request", json={"email": "unknown@example.test"})
assert response.status_code == 200 and response.json() == {"status": "ok"}
token, _ = auth.login("admin", "new-verified-password")
response = client.get("/api/auth/me", headers={"Authorization": f"Bearer {token}"})
assert response.status_code == 200
assert response.json()["email"] == "admin@example.test"
assert response.json()["email_verified"] is True

services.close()
print("\ntest_server_email_auth OK")
