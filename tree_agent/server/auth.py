"""Passwords, login sessions and account administration.

Passwords are hashed with Argon2id and never logged. A session token is 32
bytes of `secrets` randomness handed to the client once; only its SHA-256 lands
in the database, so a stolen database backup does not hand over live sessions.

There is no default account. The first `admin` is created once, against a
one-time bootstrap token the operator supplies at startup.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import sqlite3
from collections.abc import Callable
from typing import Any

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError
from argon2.low_level import Type

from . import ids
from .db import Database
from .errors import (
    AuthenticationError,
    ConflictError,
    NotFound,
    PermissionDenied,
    ValidationError,
)
from .repositories import users as users_repo
from .services.access import Actor

SESSION_TTL_MS = 30 * 24 * 60 * 60 * 1000
MIN_PASSWORD_LENGTH = 10
MAX_PASSWORD_LENGTH = 256
TOKEN_BYTES = 32
RECOVERY_CODE_TTL_MS = 10 * 60 * 1000
RECOVERY_CODE_MAX_ATTEMPTS = 5

# Argon2id at the library's defaults, which are already tuned for interactive
# logins. Kept explicit so a future change is a visible decision.
_hasher = PasswordHasher(
    time_cost=3, memory_cost=64 * 1024, parallelism=4, hash_len=32, salt_len=16, type=Type.ID
)


def hash_password(password: str) -> str:
    _validate_password(password)
    return _hasher.hash(password)


def verify_password(stored_hash: str, password: str) -> bool:
    try:
        return _hasher.verify(stored_hash, password)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


def _validate_password(password: str) -> None:
    if not isinstance(password, str) or len(password) < MIN_PASSWORD_LENGTH:
        raise ValidationError(f"密碼至少需要 {MIN_PASSWORD_LENGTH} 個字元")
    if len(password) > MAX_PASSWORD_LENGTH:
        raise ValidationError("密碼過長")


def _validate_username(username: str) -> str:
    username = (username or "").strip()
    if not 3 <= len(username) <= 64:
        raise ValidationError("使用者名稱長度需介於 3 到 64 個字元")
    if any(char.isspace() for char in username):
        raise ValidationError("使用者名稱不可含空白")
    return username


def _normalise_email(email: str) -> str:
    value = (email or "").strip().lower()
    # This intentionally checks only the shape useful to SMTP.  Full RFC 5322
    # parsing rejects real corporate addresses and does not prove deliverability.
    if len(value) > 254 or value.count("@") != 1:
        raise ValidationError("請輸入有效的電子郵件地址")
    local, domain = value.rsplit("@", 1)
    if not local or not domain or "." not in domain or any(char.isspace() for char in value):
        raise ValidationError("請輸入有效的電子郵件地址")
    return value


def new_token() -> str:
    return secrets.token_urlsafe(TOKEN_BYTES)


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def row_to_actor(row: sqlite3.Row, session_id: str | None = None) -> Actor:
    return Actor(
        id=row["id"],
        username=row["username"],
        display_name=row["display_name"],
        role=row["role"],
        is_active=bool(row["is_active"]),
        session_id=session_id,
    )


class AuthService:
    def __init__(
        self,
        db: Database,
        *,
        bootstrap_token: str | None = None,
        send_email: Callable[[str, str, str], None] | None = None,
    ) -> None:
        self.db = db
        self._send_email = send_email
        # Consumed exactly once; after that the only way to add users is an
        # authenticated admin.
        self._bootstrap_token = bootstrap_token or os.environ.get(
            "TREE_AGENT_BOOTSTRAP_TOKEN"
        )
    # ------------------------------------------------------------ bootstrap

    def needs_bootstrap(self) -> bool:
        """Return the current database state, including writes by another process.

        The local desktop service can remain alive while a prior launcher or
        recovery path writes the initial administrator.  Caching this value at
        service startup then leaves the GUI permanently stuck in bootstrap
        mode, even though the account exists.
        """
        with self.db.read() as conn:
            return users_repo.count(conn) == 0

    def bootstrap_admin(
        self, *, token: str, username: str, password: str, email: str, display_name: str = ""
    ) -> dict[str, Any]:
        if not self._bootstrap_token:
            raise PermissionDenied("這個服務沒有設定一次性初始化密碼")
        # Constant-time: a timing oracle on the bootstrap token would hand over
        # the whole workspace.
        if not hmac.compare_digest(token or "", self._bootstrap_token):
            raise PermissionDenied("初始化密碼不正確")
        username = _validate_username(username)
        email = _normalise_email(email)
        password_hash = hash_password(password)

        def job(conn: sqlite3.Connection) -> dict[str, Any]:
            if users_repo.count(conn) != 0:
                raise ConflictError("已經有使用者存在，無法再次初始化")
            if users_repo.get_by_email(conn, email) is not None:
                raise ConflictError("這個電子郵件地址已被使用")
            now = ids.now_ms()
            user_id = users_repo.create(
                conn,
                username=username,
                password_hash=password_hash,
                display_name=display_name.strip() or username,
                role="admin",
                email=email,
                now_ms=now,
            )
            return users_repo.get(conn, user_id)

        user = self.db.write(job, label="bootstrap_admin")
        self._bootstrap_token = None
        return user

    # ---------------------------------------------------------------- login

    def login(self, username: str, password: str) -> tuple[str, dict[str, Any]]:
        """Returns (token, user). Wrong user and wrong password are one error."""
        with self.db.read() as conn:
            row = users_repo.get_by_username(conn, (username or "").strip())
        stored = row["password_hash"] if row else None
        # Always spend the hashing time, so a missing account and a wrong
        # password take the same wall-clock.
        ok = verify_password(stored, password) if stored else _dummy_verify(password)
        if not row or not ok:
            raise AuthenticationError("帳號或密碼不正確")
        if not row["is_active"] or row["deleted_at_ms"] is not None:
            raise AuthenticationError("這個帳號已停用")

        token = new_token()
        digest = token_hash(token)
        user_id = row["id"]

        def job(conn: sqlite3.Connection) -> None:
            now = ids.now_ms()
            users_repo.create_session(
                conn,
                user_id=user_id,
                token_hash=digest,
                now_ms=now,
                expires_at_ms=now + SESSION_TTL_MS,
            )

        self.db.write(job, label="login")
        return token, users_repo.row_to_user(row)

    def authenticate(self, token: str | None) -> Actor:
        if not token:
            raise AuthenticationError("需要登入")
        digest = token_hash(token)
        now = ids.now_ms()
        with self.db.read() as conn:
            row = users_repo.session_by_token(conn, digest, now)
        if row is None:
            raise AuthenticationError("登入已失效，請重新登入")
        if not row["is_active"] or row["deleted_at_ms"] is not None:
            raise AuthenticationError("這個帳號已停用")
        session_id = row["session_id"]
        # One write per minute at most; every request would serialise the whole
        # API behind the writer queue for a field nothing reads in real time.
        if now - row["last_seen_at_ms"] > 60_000:
            self.db.write(
                lambda conn: users_repo.touch_session(conn, session_id, now),
                label="touch_session",
            )
        return row_to_actor(row, session_id)

    def logout(self, actor: Actor) -> None:
        if not actor.session_id:
            return
        session_id = actor.session_id
        self.db.write(
            lambda conn: users_repo.revoke_session(conn, session_id, ids.now_ms()),
            label="logout",
        )

    # ------------------------------------------------------------ accounts

    def list_users(self, actor: Actor) -> list[dict[str, Any]]:
        if not actor.is_admin:
            raise PermissionDenied("需要系統管理員權限")
        with self.db.read() as conn:
            return users_repo.list_all(conn)

    def create_user(
        self, actor: Actor, *, username: str, password: str, display_name: str, role: str,
        email: str | None = None,
    ) -> dict[str, Any]:
        if not actor.is_admin:
            raise PermissionDenied("需要系統管理員權限")
        if role not in users_repo.ROLES:
            raise ValidationError(f"未知的角色：{role}")
        username = _validate_username(username)
        email = _normalise_email(email) if email else None
        password_hash = hash_password(password)

        def job(conn: sqlite3.Connection) -> dict[str, Any]:
            if users_repo.get_by_username(conn, username) is not None:
                raise ConflictError("這個使用者名稱已存在")
            if email and users_repo.get_by_email(conn, email) is not None:
                raise ConflictError("這個電子郵件地址已被使用")
            user_id = users_repo.create(
                conn,
                username=username,
                password_hash=password_hash,
                display_name=(display_name or "").strip() or username,
                role=role,
                email=email,
                now_ms=ids.now_ms(),
            )
            return users_repo.get(conn, user_id)

        return self.db.write(job, label="create_user")

    def set_active(self, actor: Actor, user_id: str, active: bool) -> dict[str, Any]:
        if not actor.is_admin:
            raise PermissionDenied("需要系統管理員權限")
        if user_id == actor.id and not active:
            raise ValidationError("不能停用自己的帳號")

        def job(conn: sqlite3.Connection) -> dict[str, Any]:
            now = ids.now_ms()
            if users_repo.update_fields(conn, user_id, {"is_active": 1 if active else 0}, now) == 0:
                raise NotFound("找不到使用者")
            if not active:
                users_repo.revoke_sessions_for_user(conn, user_id, now)
            return users_repo.get(conn, user_id)

        return self.db.write(job, label="set_active")

    def set_role(self, actor: Actor, user_id: str, role: str) -> dict[str, Any]:
        if not actor.is_admin:
            raise PermissionDenied("需要系統管理員權限")
        if role not in users_repo.ROLES:
            raise ValidationError(f"未知的角色：{role}")

        def job(conn: sqlite3.Connection) -> dict[str, Any]:
            if user_id == actor.id and role != "admin" and _last_admin(conn, user_id):
                raise ValidationError("至少要保留一位系統管理員")
            if users_repo.update_fields(conn, user_id, {"role": role}, ids.now_ms()) == 0:
                raise NotFound("找不到使用者")
            return users_repo.get(conn, user_id)

        return self.db.write(job, label="set_role")

    def reset_password(self, actor: Actor, user_id: str, password: str) -> None:
        """Admins reset anyone; everyone else may only change their own."""
        if not actor.is_admin and actor.id != user_id:
            raise PermissionDenied("只能變更自己的密碼")
        password_hash = hash_password(password)

        def job(conn: sqlite3.Connection) -> None:
            now = ids.now_ms()
            if users_repo.update_fields(conn, user_id, {"password_hash": password_hash}, now) == 0:
                raise NotFound("找不到使用者")
            # Every other session for that account is now stale.
            users_repo.revoke_sessions_for_user(conn, user_id, now)

        self.db.write(job, label="reset_password")

    def change_own_password(self, actor: Actor, current: str, new: str) -> None:
        with self.db.read() as conn:
            row = users_repo.get_by_username(conn, actor.username)
        if row is None or not verify_password(row["password_hash"], current):
            raise AuthenticationError("目前的密碼不正確")
        self.reset_password(actor, actor.id, new)

    # -------------------------------------------------------- email recovery

    def request_password_reset(self, email: str) -> None:
        """Mail one password-reset code without revealing whether it exists."""
        try:
            normalised = _normalise_email(email)
        except ValidationError:
            return
        with self.db.read() as conn:
            user = users_repo.get_by_email(conn, normalised)
        if not user or user["deleted_at_ms"] is not None or not user["is_active"]:
            return
        if user["email_verified_at_ms"] is None:
            return
        self._issue_and_deliver_code(
            user_id=user["id"], email=normalised, purpose="password_reset",
            subject="Tree Agent 密碼重設驗證碼",
            text="您正在重設 Tree Agent 密碼。驗證碼：{code}\n\n此驗證碼 10 分鐘內有效，最多可嘗試 5 次。若非您本人操作，請忽略此信。",
        )

    def user_email(self, user_id: str) -> dict[str, Any]:
        with self.db.read() as conn:
            user = users_repo.get(conn, user_id)
        if user is None:
            raise AuthenticationError("帳號不存在")
        return {"email": user["email"], "email_verified": user["email_verified"]}

    def confirm_password_reset(self, *, email: str, code: str, password: str) -> None:
        normalised = _normalise_email(email)
        password_hash = hash_password(password)

        def job(conn: sqlite3.Connection) -> bool:
            now = ids.now_ms()
            record = users_repo.active_recovery_code(
                conn, email=normalised, purpose="password_reset", now_ms=now
            )
            if record is None or not _matches_code(record, code):
                if record is not None:
                    _record_failed_code_attempt(conn, record, now)
                return False
            if users_repo.consume_recovery_code(conn, record["id"], now) == 0:
                return False
            if users_repo.update_fields(conn, record["user_id"], {"password_hash": password_hash}, now) == 0:
                return False
            users_repo.revoke_sessions_for_user(conn, record["user_id"], now)
            return True

        if not self.db.write(job, label="confirm_password_reset"):
            raise ValidationError("驗證碼無效、已過期或嘗試次數已達上限")

    def request_email_verification(self, actor: Actor, email: str) -> None:
        normalised = _normalise_email(email)
        with self.db.read() as conn:
            existing = users_repo.get_by_email(conn, normalised)
        if existing is not None and existing["id"] != actor.id:
            raise ConflictError("這個電子郵件地址已被使用")
        self._issue_and_deliver_code(
            user_id=actor.id, email=normalised, purpose="email_verify",
            subject="Tree Agent 電子郵件驗證碼",
            text="您正在驗證 Tree Agent 的電子郵件地址。驗證碼：{code}\n\n此驗證碼 10 分鐘內有效，最多可嘗試 5 次。若非您本人操作，請忽略此信。",
        )

    def confirm_email_verification(self, actor: Actor, *, email: str, code: str) -> dict[str, Any]:
        normalised = _normalise_email(email)

        def job(conn: sqlite3.Connection) -> dict[str, Any] | None:
            now = ids.now_ms()
            record = users_repo.active_recovery_code(
                conn, email=normalised, purpose="email_verify", now_ms=now
            )
            if record is None or record["user_id"] != actor.id or not _matches_code(record, code):
                if record is not None and record["user_id"] == actor.id:
                    _record_failed_code_attempt(conn, record, now)
                return None
            existing = users_repo.get_by_email(conn, normalised)
            if existing is not None and existing["id"] != actor.id:
                raise ConflictError("這個電子郵件地址已被使用")
            if users_repo.consume_recovery_code(conn, record["id"], now) == 0:
                return None
            users_repo.update_fields(
                conn, actor.id, {"email": normalised, "email_verified_at_ms": now}, now
            )
            return users_repo.get(conn, actor.id)  # type: ignore[return-value]

        verified = self.db.write(job, label="confirm_email_verification")
        if verified is None:
            raise ValidationError("驗證碼無效、已過期或嘗試次數已達上限")
        return verified

    def _issue_and_deliver_code(
        self, *, user_id: str, email: str, purpose: str, subject: str, text: str
    ) -> None:
        if self._send_email is None:
            raise ValidationError("郵件服務尚未設定")
        code = f"{secrets.randbelow(1_000_000):06d}"
        salt = secrets.token_urlsafe(16)
        digest = _code_hash(salt, code)

        def job(conn: sqlite3.Connection) -> str:
            now = ids.now_ms()
            users_repo.revoke_codes(conn, user_id=user_id, purpose=purpose, now_ms=now)
            return users_repo.create_recovery_code(
                conn, user_id=user_id, purpose=purpose, email=email, code_salt=salt,
                code_hash=digest, expires_at_ms=now + RECOVERY_CODE_TTL_MS, now_ms=now,
            )

        code_id = self.db.write(job, label=f"issue_{purpose}_code")
        try:
            self._send_email(email, subject, text.format(code=code))
        except Exception as exc:  # noqa: BLE001 - SMTP libraries vary by platform
            self.db.write(
                lambda conn: users_repo.consume_recovery_code(conn, code_id, ids.now_ms()),
                label=f"revoke_undelivered_{purpose}_code",
            )
            raise ValidationError("無法寄送驗證碼，請稍後再試") from exc


def _last_admin(conn: sqlite3.Connection, user_id: str) -> bool:
    remaining = conn.execute(
        "SELECT count(*) FROM users WHERE role = 'admin' AND is_active = 1"
        " AND deleted_at_ms IS NULL AND id <> ?",
        (user_id,),
    ).fetchone()[0]
    return remaining == 0


def _code_hash(salt: str, code: str) -> str:
    return hashlib.sha256(f"{salt}:{code}".encode("utf-8")).hexdigest()


def _matches_code(record: sqlite3.Row, code: str) -> bool:
    candidate = _code_hash(record["code_salt"], (code or "").strip())
    return hmac.compare_digest(record["code_hash"], candidate)


def _record_failed_code_attempt(conn: sqlite3.Connection, record: sqlite3.Row, now_ms: int) -> None:
    users_repo.increment_recovery_attempts(conn, record["id"])
    if record["attempts"] + 1 >= record["max_attempts"]:
        users_repo.consume_recovery_code(conn, record["id"], now_ms)


_DUMMY_HASH = _hasher.hash("tree-agent-timing-equaliser")


def _dummy_verify(password: str) -> bool:
    """Burn the same CPU as a real verification for an unknown username."""
    try:
        _hasher.verify(_DUMMY_HASH, password or "")
    except Exception:  # noqa: BLE001 - the result is deliberately discarded
        pass
    return False
