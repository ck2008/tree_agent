"""The storage layer itself: pragmas, migrations, the writer queue, backups.

SQLite takes one writer at a time. The writer queue is what turns that from a
source of `database is locked` errors into an ordinary queue, so most of this
suite is about proving it actually serialises.
"""

from __future__ import annotations

import os
import sqlite3
import tempfile
import threading

from server_support import banner

from tree_agent.server.db import CONNECTION_PRAGMAS, Database, DatabaseError
from tree_agent.server.app import Config

home = tempfile.mkdtemp(prefix="tree-agent-db-")
db = Database(os.path.join(home, "tree-agent.db"))

banner("migrations apply once and are recorded")
applied = db.migrate()
print("applied:", applied)
assert applied == [
    "0001_initial.sql", "0002_user_email_password_reset.sql", "0003_mail_settings.sql",
]
assert db.migrate() == [], "a second run must be a no-op"
with db.read() as conn:
    recorded = conn.execute("SELECT version, name FROM schema_migrations").fetchall()
    tables = {
        row[0]
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
    }
assert [tuple(row) for row in recorded] == [
    (1, "0001_initial.sql"),
    (2, "0002_user_email_password_reset.sql"),
    (3, "0003_mail_settings.sql"),
]
for required in (
    "users",
    "auth_sessions",
    "password_reset_codes",
    "mail_settings",
    "idempotency_keys",
    "projects",
    "project_memberships",
    "conversations",
    "messages",
    "tool_calls",
    "attachments",
    "attachment_chunks",
    "message_attachments",
    "attachment_uploads",
    "attachment_upload_chunks",
    "migration_reports",
    "project_fts",
    "conversation_fts",
    "message_fts",
):
    assert required in tables, required
print(len([t for t in tables if not t.endswith(("_data", "_idx", "_content", "_docsize", "_config"))]), "base tables")

banner("every connection gets the pragmas the spec requires")
with db.read() as conn:
    settings = {
        "journal_mode": conn.execute("PRAGMA journal_mode").fetchone()[0],
        "foreign_keys": conn.execute("PRAGMA foreign_keys").fetchone()[0],
        "synchronous": conn.execute("PRAGMA synchronous").fetchone()[0],
        "busy_timeout": conn.execute("PRAGMA busy_timeout").fetchone()[0],
    }
print(settings)
assert settings["journal_mode"] == "wal"
assert settings["foreign_keys"] == 1
assert settings["synchronous"] == 2  # FULL
assert settings["busy_timeout"] == 10000
assert any("synchronous = FULL" in pragma for pragma in CONNECTION_PRAGMAS)

banner("the writer queue serialises everything, from every thread")
db.write(lambda conn: conn.execute("CREATE TABLE counter (n INTEGER)"))
db.write(lambda conn: conn.execute("INSERT INTO counter VALUES (0)"))


def bump() -> None:
    for _ in range(50):
        # Read-modify-write inside one job: without serialisation these lose
        # updates, and the final total is the proof either way.
        db.write(
            lambda conn: conn.execute(
                "UPDATE counter SET n = (SELECT n FROM counter) + 1"
            )
        )


threads = [threading.Thread(target=bump) for _ in range(20)]
[t.start() for t in threads]
[t.join() for t in threads]
with db.read() as conn:
    total = conn.execute("SELECT n FROM counter").fetchone()[0]
print("20 threads x 50 increments =", total)
assert total == 1000, total

banner("a job that raises leaves nothing behind")


def half_written(conn: sqlite3.Connection) -> None:
    conn.execute("INSERT INTO counter VALUES (999)")
    raise RuntimeError("simulated failure")


try:
    db.write(half_written)
except RuntimeError as exc:
    print("rolled back after:", exc)
else:
    raise AssertionError("the failing job should have re-raised")
with db.read() as conn:
    assert conn.execute("SELECT count(*) FROM counter WHERE n = 999").fetchone()[0] == 0

banner("reads run while a write is in flight rather than queueing behind it")
started, release = threading.Event(), threading.Event()


def slow_write(conn: sqlite3.Connection) -> None:
    started.set()
    release.wait(10)


writer = threading.Thread(target=lambda: db.write(slow_write))
writer.start()
assert started.wait(5)
with db.read() as conn:
    assert conn.execute("SELECT n FROM counter").fetchone()[0] == 1000
print("read completed while the writer held the lock")
release.set()
writer.join(10)

banner("backups are consistent and verifiable")
backup = db.backup(os.path.join(home, "backups", "copy.db"))
assert os.path.getsize(backup) > 0
copy = sqlite3.connect(backup)
try:
    assert copy.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert copy.execute("SELECT n FROM counter").fetchone()[0] == 1000
finally:
    copy.close()
print("backup:", os.path.basename(backup), os.path.getsize(backup), "bytes, integrity ok")
assert db.integrity_check() == "ok"

banner("the database refuses to live on a network path")
for path in (r"\\nas\share\tree-agent.db", "//nas/share/tree-agent.db"):
    try:
        Database(path)
    except DatabaseError as exc:
        print("refused:", path)
        assert "網路磁碟" in exc.detail
    else:
        raise AssertionError(f"{path} should have been refused")

banner("the plain HTTP service only binds loopback; a reverse proxy owns HTTPS")
for host in ("0.0.0.0", "192.168.1.10", "example.test"):
    try:
        Config(host=host)
    except ValueError as exc:
        assert "loopback" in str(exc)
        print("refused listener:", host)
    else:
        raise AssertionError(f"{host} should not be a direct listener")
for host in ("localhost", "127.0.0.1", "::1"):
    assert Config(host=host).host == host

db.close()
print("\ntest_server_db OK")
