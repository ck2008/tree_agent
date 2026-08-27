"""Admin SMTP settings are safe to read, encrypted at rest and hot-reloaded."""

from __future__ import annotations

from server_support import ADMIN_PASSWORD, banner, make_admin, make_http, make_services, make_user

from tree_agent.server import mailer


services = make_services()
admin = make_admin(services)
member = make_user(services, admin, "member")
client = make_http(services)


def token_for(username: str, password: str) -> dict[str, str]:
    token, _ = services.auth.login(username, password)
    return {"Authorization": f"Bearer {token}"}


admin_headers = token_for("admin", ADMIN_PASSWORD)
member_headers = token_for("member", "member-password")

banner("only an administrator can inspect or change mail settings")
assert client.get("/api/admin/mail-settings").status_code == 401
assert client.get("/api/admin/mail-settings", headers=member_headers).status_code == 403
initial = client.get("/api/admin/mail-settings", headers=admin_headers)
assert initial.status_code == 200
assert initial.json() == {
    "host": "127.0.0.1", "port": 25, "from_address": "ck@eic.com.tw",
    "encryption": "none", "username": "", "has_password": False,
}

banner("a remote SMTP host, STARTTLS and SMTP AUTH save without exposing the password")
updated = client.put(
    "/api/admin/mail-settings",
    headers=admin_headers,
    json={
        "host": "smtp.example.test", "port": 587, "from_address": "robot@example.test",
        "encryption": "starttls", "username": "robot", "password": "highly-secret-password",
    },
)
assert updated.status_code == 200, updated.text
assert updated.json() == {
    "host": "smtp.example.test", "port": 587, "from_address": "robot@example.test",
    "encryption": "starttls", "username": "robot", "has_password": True,
}
assert "password" not in updated.json()
with services.db.read() as conn:
    protected = conn.execute("SELECT password_protected FROM mail_settings WHERE id = 1").fetchone()[0]
assert protected != "highly-secret-password" and "highly-secret-password" not in protected
assert protected.startswith(("dpapi:", "fernet:"))


class FakeSMTP:
    instances: list["FakeSMTP"] = []

    def __init__(self, host, port, timeout):
        self.host, self.port, self.timeout = host, port, timeout
        self.calls = []
        self.__class__.instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def ehlo(self):
        self.calls.append(("ehlo",))

    def starttls(self, *, context):
        assert context is not None
        self.calls.append(("starttls",))

    def login(self, username, password):
        self.calls.append(("login", username, password))

    def send_message(self, message):
        self.calls.append(("send", message["To"], message["From"]))


original_smtp, original_ssl = mailer.smtplib.SMTP, mailer.smtplib.SMTP_SSL
mailer.smtplib.SMTP = FakeSMTP
mailer.smtplib.SMTP_SSL = FakeSMTP
try:
    banner("a test message uses the newly saved STARTTLS and AUTH settings immediately")
    sent = client.post(
        "/api/admin/mail-settings/test", headers=admin_headers,
        json={"recipient": "recipient@example.test"},
    )
    assert sent.status_code == 200, sent.text
    smtp = FakeSMTP.instances[-1]
    assert smtp.host == "smtp.example.test" and smtp.port == 587
    assert smtp.calls == [
        ("ehlo",), ("starttls",), ("ehlo",),
        ("login", "robot", "highly-secret-password"),
        ("send", "recipient@example.test", "robot@example.test"),
    ]

    banner("an omitted password keeps the protected secret, and SSL hot-reloads too")
    switched = client.put(
        "/api/admin/mail-settings", headers=admin_headers,
        json={
            "host": "mail.example.test", "port": 465, "from_address": "robot@example.test",
            "encryption": "ssl", "username": "robot",
        },
    )
    assert switched.status_code == 200 and switched.json()["has_password"] is True
    assert client.post(
        "/api/admin/mail-settings/test", headers=admin_headers,
        json={"recipient": "recipient@example.test"},
    ).status_code == 200
    assert FakeSMTP.instances[-1].host == "mail.example.test"
    assert ("login", "robot", "highly-secret-password") in FakeSMTP.instances[-1].calls
finally:
    mailer.smtplib.SMTP, mailer.smtplib.SMTP_SSL = original_smtp, original_ssl

banner("invalid hosts, mismatched authentication and malformed test recipients are refused")
bad_host = client.put(
    "/api/admin/mail-settings", headers=admin_headers,
    json={"host": "bad host", "port": 25, "from_address": "robot@example.test"},
)
assert bad_host.status_code == 400
no_password = client.put(
    "/api/admin/mail-settings", headers=admin_headers,
    json={
        "host": "mail.example.test", "port": 25, "from_address": "robot@example.test",
        "username": "requires-a-password", "password": "",
    },
)
assert no_password.status_code == 400
assert client.post(
    "/api/admin/mail-settings/test", headers=admin_headers,
    json={"recipient": "not-an-email"},
).status_code == 400
assert client.post(
    "/api/admin/mail-settings/test", headers=member_headers,
    json={"recipient": "recipient@example.test"},
).status_code == 403

services.close()
print("\ntest_server_mail_settings OK")
