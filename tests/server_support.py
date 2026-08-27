"""Shared setup for the server test suites.

Each suite gets its own temporary database and its own admin, so they can be run
in any order or on their own. `serve()` starts a real uvicorn on a free port for
the tests that need to exercise the actual HTTP client rather than the in-process
ASGI shortcut.
"""

from __future__ import annotations

import contextlib
import os
import socket
import sys
import tempfile
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tree_agent.server.app import Config, Services, create_app  # noqa: E402

ADMIN_PASSWORD = "tree-agent-admin-pw"
BOOTSTRAP_TOKEN = "test-bootstrap-token"


def make_services(home: str | None = None, **overrides) -> Services:
    home = home or tempfile.mkdtemp(prefix="tree-agent-test-")
    config = Config(
        db_path=os.path.join(home, "tree-agent.db"),
        secure_cookies=False,
        bootstrap_token=BOOTSTRAP_TOKEN,
        run_sweeper=False,
        **overrides,
    )
    services = Services(config)
    services.home = home
    return services


def make_admin(services: Services, username: str = "admin"):
    """Create the first admin and return it as an `Actor`."""
    from tree_agent.server.auth import row_to_actor
    from tree_agent.server.repositories import users as users_repo

    services.auth.bootstrap_admin(
        token=BOOTSTRAP_TOKEN, username=username, password=ADMIN_PASSWORD,
        email=f"{username}@example.test",
    )
    with services.db.read() as conn:
        return row_to_actor(users_repo.get_by_username(conn, username))


def make_user(services: Services, admin, username: str, role: str = "member"):
    from tree_agent.server.auth import row_to_actor
    from tree_agent.server.repositories import users as users_repo

    services.auth.create_user(
        admin, username=username, password=f"{username}-password", display_name=username, role=role
    )
    with services.db.read() as conn:
        return row_to_actor(users_repo.get_by_username(conn, username))


def make_http(services: Services):
    """An in-process ASGI client — fast, and enough for everything but streaming."""
    from fastapi.testclient import TestClient

    return TestClient(create_app(services.config, services))


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


@contextlib.contextmanager
def serve(services: Services):
    """Run a real uvicorn against `services`; yields the base URL."""
    import uvicorn

    port = free_port()
    config = uvicorn.Config(
        create_app(services.config, services),
        host="127.0.0.1",
        port=port,
        log_level="warning",
        lifespan="on",
    )
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, name="test-uvicorn", daemon=True)
    thread.start()
    deadline = time.monotonic() + 20
    while not server.started:
        if time.monotonic() > deadline:
            raise RuntimeError("uvicorn did not start in time")
        time.sleep(0.05)
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        thread.join(timeout=15)


def expect_error(status: int, fn, *args, **kwargs):
    """Assert that a service call fails with `status`, and return the error."""
    from tree_agent.server.errors import ServiceError

    try:
        fn(*args, **kwargs)
    except ServiceError as exc:
        assert exc.status == status, f"expected {status}, got {exc.status}: {exc.detail}"
        return exc
    raise AssertionError(f"expected a {status} from {getattr(fn, '__name__', fn)}, got success")


def banner(title: str) -> None:
    print(f"\n--- {title}")
