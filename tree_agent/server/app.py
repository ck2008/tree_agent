"""Service host: configuration, the service container, and the ASGI app.

    python -m tree_agent.server --db D:\\TreeAgentData\\tree-agent.db

One process owns the database. Do not run two of these against the same file,
and do not put the file on a share — `db.py` refuses the second mistake and
this module's docstring is the only warning you get about the first.
"""

from __future__ import annotations

import argparse
import contextlib
import ipaddress
import logging
import os
import secrets
import threading
from dataclasses import dataclass, field
from collections.abc import Callable
from typing import Any, AsyncIterator

from fastapi import FastAPI

from .api import build_router, install_error_handler
from .auth import AuthService
from .db import Database
from .mail_settings import MailSettings, MailSettingsService
from .services.attachments import AttachmentService
from .services.idempotency import IdempotencyService
from .services.maintenance import MaintenanceService
from .services.messages import MessageService
from .services.search import SearchService
from .services.tree import TreeService

log = logging.getLogger("tree_agent.server")

DEFAULT_DB_PATH = os.environ.get(
    "TREE_AGENT_DB", os.path.join(os.path.expanduser("~"), ".tree_agent", "tree-agent.db")
)

# Mirrors the desktop app's shipped defaults, so an imported workspace resolves
# to the same settings it did before. `sandbox` is deliberately `no-sandbox`:
# Codex's own default cannot run on a mapped network drive.
WORKSPACE_DEFAULTS = {
    "cwd": None,
    "model": None,
    "sandbox": "no-sandbox",
    "claude_permission": "default",
}

SWEEP_INTERVAL_SECONDS = 3600


@dataclass
class Config:
    db_path: str = DEFAULT_DB_PATH
    host: str = "127.0.0.1"
    port: int = 8765
    # Off only for local HTTP development; a session cookie without it travels
    # in clear text the first time a client talks to a plain-HTTP endpoint.
    secure_cookies: bool = True
    allow_network_path: bool = False
    bootstrap_token: str | None = None
    smtp_host: str = "127.0.0.1"
    smtp_port: int = 25
    smtp_from: str = "ck@eic.com.tw"
    # Tests inject this seam; production falls back to the local MTA above.
    mail_sender: Callable[[str, str, str], None] | None = None
    defaults: dict[str, Any] = field(default_factory=lambda: dict(WORKSPACE_DEFAULTS))
    run_sweeper: bool = True

    def __post_init__(self) -> None:
        if not is_loopback_host(self.host):
            raise ValueError(
                "Tree Agent service must listen on loopback only. Put an HTTPS reverse "
                "proxy in front of it for remote users; --host may only be localhost, "
                "127.0.0.1, or ::1."
            )


def is_loopback_host(host: str) -> bool:
    """Whether Uvicorn may bind this address directly.

    The service intentionally has no TLS listener.  A reverse proxy terminates
    HTTPS and forwards to this loopback-only socket, preventing bearer tokens
    and transcripts from ever being exposed by a plain HTTP public listener.
    """
    candidate = host.strip().lower().strip("[]")
    if candidate == "localhost":
        return True
    try:
        return ipaddress.ip_address(candidate).is_loopback
    except ValueError:
        return False


class Services:
    """Everything the routes are allowed to touch, built once per process."""

    def __init__(self, config: Config) -> None:
        self.config = config
        self.db = Database(config.db_path, allow_network_path=config.allow_network_path)
        self.db.migrate()
        self.mail_settings = MailSettingsService(
            self.db,
            defaults=MailSettings(
                host=config.smtp_host, port=config.smtp_port, from_address=config.smtp_from,
            ),
            send_override=config.mail_sender,
        )
        self.auth = AuthService(
            self.db, bootstrap_token=config.bootstrap_token, send_email=self.mail_settings.send
        )
        self.tree = TreeService(self.db, defaults=config.defaults)
        self.messages = MessageService(self.db)
        self.attachments = AttachmentService(self.db)
        self.search = SearchService(self.db)
        self.maintenance = MaintenanceService(self.db)
        self.idempotency = IdempotencyService(self.db)
        self._stop = threading.Event()
        self._sweeper: threading.Thread | None = None

    def start_sweeper(self) -> None:
        if not self.config.run_sweeper or self._sweeper is not None:
            return

        def loop() -> None:
            while not self._stop.wait(SWEEP_INTERVAL_SECONDS):
                try:
                    result = self.maintenance.sweep()
                    if any(result.values()):
                        log.info("sweep: %s", result)
                except Exception:  # noqa: BLE001 - a sweep failure must not kill the service
                    log.exception("sweep failed")

        self._sweeper = threading.Thread(target=loop, name="tree-agent-sweeper", daemon=True)
        self._sweeper.start()

    def close(self) -> None:
        self._stop.set()
        if self._sweeper is not None:
            self._sweeper.join(timeout=5)
            self._sweeper = None
        self.db.close()


def create_app(config: Config | None = None, services: Services | None = None) -> FastAPI:
    config = config or Config()
    services = services or Services(config)

    @contextlib.asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        services.maintenance.sweep()
        services.start_sweeper()
        if services.auth.needs_bootstrap():
            log.warning(
                "沒有任何使用者。請用 POST /api/auth/bootstrap 搭配一次性初始化密碼建立第一位管理員。"
            )
        try:
            yield
        finally:
            services.close()

    app = FastAPI(
        title="Tree Agent Workspace",
        lifespan=lifespan,
        version="1.0",
        # No docs UI by default: this service holds every transcript in the
        # organisation and does not need an unauthenticated schema browser.
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.state.services = services
    install_error_handler(app)
    app.include_router(build_router(services))

    return app


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Tree Agent 共用工作區服務")
    parser.add_argument("--db", default=DEFAULT_DB_PATH, help="SQLite 檔案路徑（必須是本機磁碟）")
    parser.add_argument(
        "--host",
        default="127.0.0.1",
        help="僅限 loopback（localhost、127.0.0.1 或 ::1）；遠端存取請使用 HTTPS reverse proxy",
    )
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument(
        "--insecure-cookies",
        action="store_true",
        help="僅供本機 HTTP 測試：不要在正式環境使用",
    )
    parser.add_argument(
        "--allow-network-path",
        action="store_true",
        help="覆寫網路磁碟檢查（不建議，SQLite 在 SMB 上的鎖並不可靠）",
    )
    parser.add_argument("--cwd-default", default=None, help="新專案的預設工作目錄")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s"
    )

    defaults = dict(WORKSPACE_DEFAULTS)
    if args.cwd_default:
        defaults["cwd"] = args.cwd_default

    bootstrap_token = os.environ.get("TREE_AGENT_BOOTSTRAP_TOKEN")
    config = Config(
        db_path=args.db,
        host=args.host,
        port=args.port,
        secure_cookies=not args.insecure_cookies,
        allow_network_path=args.allow_network_path,
        bootstrap_token=bootstrap_token,
        defaults=defaults,
    )
    services = Services(config)
    if services.auth.needs_bootstrap() and not bootstrap_token:
        # Printed once, to the operator's console, and never stored.
        config.bootstrap_token = secrets.token_urlsafe(24)
        services.auth = AuthService(
            services.db,
            bootstrap_token=config.bootstrap_token,
            send_email=services.mail_settings.send,
        )
        print("=" * 72)
        print("尚未建立任何使用者。用這組一次性初始化密碼建立第一位管理員：")
        print(f"    {config.bootstrap_token}")
        print("    POST /api/auth/bootstrap {token, username, password}")
        print("=" * 72, flush=True)

    import uvicorn

    uvicorn.run(create_app(config, services), host=config.host, port=config.port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
