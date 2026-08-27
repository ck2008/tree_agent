"""Persistent, admin-controlled SMTP settings without plaintext secrets."""

from __future__ import annotations

import base64
import ctypes
from ctypes import wintypes
import os
import re
import secrets
import sqlite3
import threading
from dataclasses import dataclass
from email.utils import parseaddr
from pathlib import Path
from typing import Any, Callable

from cryptography.fernet import Fernet, InvalidToken

from . import ids
from .db import Database
from .errors import ValidationError
from .mailer import SmtpMailer

ENCRYPTION_MODES = ("none", "starttls", "ssl")
_HOST_LABEL = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$", re.IGNORECASE)
_CRYPTO_UI_FORBIDDEN = 0x1


@dataclass(frozen=True)
class MailSettings:
    host: str
    port: int
    from_address: str
    encryption: str = "none"
    username: str = ""
    password_protected: str | None = None

    def safe(self) -> dict[str, Any]:
        return {
            "host": self.host,
            "port": self.port,
            "from_address": self.from_address,
            "encryption": self.encryption,
            "username": self.username,
            "has_password": self.password_protected is not None,
        }


class _DataBlob(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_byte))]


def _blob(value: bytes) -> tuple[_DataBlob, ctypes.Array[ctypes.c_char]]:
    buffer = ctypes.create_string_buffer(value)
    return _DataBlob(len(value), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_byte))), buffer


class SecretProtector:
    """Uses the service account's DPAPI profile on Windows.

    A non-Windows development host gets a Fernet key placed beside the local
    database.  The key is intentionally not in SQLite, so copying a database
    does not copy the SMTP credential.  Windows deployments use DPAPI instead.
    """

    def __init__(self, db_path: str) -> None:
        self._key_path = Path(db_path).with_name("tree-agent-mail-settings.key")
        self._fernet: Fernet | None = None

    def protect(self, value: str) -> str:
        raw = value.encode("utf-8")
        if os.name == "nt":
            source, _source_buffer = _blob(raw)
            output = _DataBlob()
            if not ctypes.windll.crypt32.CryptProtectData(
                ctypes.byref(source), "Tree Agent SMTP password", None, None, None,
                _CRYPTO_UI_FORBIDDEN, ctypes.byref(output),
            ):
                raise ctypes.WinError()
            try:
                return "dpapi:" + base64.b64encode(
                    ctypes.string_at(output.pbData, output.cbData)
                ).decode("ascii")
            finally:
                ctypes.windll.kernel32.LocalFree(output.pbData)
        return "fernet:" + self._get_fernet().encrypt(raw).decode("ascii")

    def unprotect(self, protected: str) -> str:
        try:
            prefix, encoded = protected.split(":", 1)
            if prefix == "dpapi":
                if os.name != "nt":
                    raise ValueError("這份 SMTP 密碼只能在原本的 Windows 服務主機上使用")
                source, _source_buffer = _blob(base64.b64decode(encoded, validate=True))
                output = _DataBlob()
                if not ctypes.windll.crypt32.CryptUnprotectData(
                    ctypes.byref(source), None, None, None, None, _CRYPTO_UI_FORBIDDEN,
                    ctypes.byref(output),
                ):
                    raise ctypes.WinError()
                try:
                    return ctypes.string_at(output.pbData, output.cbData).decode("utf-8")
                finally:
                    ctypes.windll.kernel32.LocalFree(output.pbData)
            if prefix == "fernet":
                return self._get_fernet().decrypt(encoded.encode("ascii")).decode("utf-8")
        except (ValueError, UnicodeDecodeError, InvalidToken) as exc:
            raise ValidationError("SMTP 密碼無法在這台服務主機上解密，請重新輸入") from exc
        raise ValidationError("SMTP 密碼格式無效，請重新輸入")

    def _get_fernet(self) -> Fernet:
        if self._fernet is not None:
            return self._fernet
        try:
            key = self._key_path.read_bytes()
        except FileNotFoundError:
            self._key_path.parent.mkdir(parents=True, exist_ok=True)
            key = base64.urlsafe_b64encode(secrets.token_bytes(32))
            temporary = self._key_path.with_suffix(".tmp")
            temporary.write_bytes(key)
            try:
                os.chmod(temporary, 0o600)
            except OSError:
                pass
            os.replace(temporary, self._key_path)
        self._fernet = Fernet(key)
        return self._fernet


def validate_host(host: str) -> str:
    value = (host or "").strip()
    if not value or len(value) > 253 or any(char.isspace() or ord(char) < 32 for char in value):
        raise ValidationError("SMTP 主機無效")
    candidate = value.strip("[]")
    # IP literals are useful for internal relays.  Other hosts must have sane
    # DNS labels; DNS resolution is deliberately left to SMTP at send time.
    try:
        import ipaddress
        ipaddress.ip_address(candidate)
        return candidate
    except ValueError:
        labels = candidate.rstrip(".").split(".")
        if not labels or any(not _HOST_LABEL.fullmatch(label) for label in labels):
            raise ValidationError("SMTP 主機無效")
        return candidate


def validate_email(value: str, *, label: str = "電子郵件地址") -> str:
    address = (value or "").strip()
    parsed_name, parsed_address = parseaddr(address)
    if parsed_name or parsed_address != address or len(address) > 254:
        raise ValidationError(f"請輸入有效的{label}")
    local, separator, domain = address.rpartition("@")
    if not separator or not local or not domain or any(char.isspace() for char in address):
        raise ValidationError(f"請輸入有效的{label}")
    return address


class MailSettingsService:
    def __init__(
        self,
        db: Database,
        *,
        defaults: MailSettings,
        send_override: Callable[[str, str, str], None] | None = None,
    ) -> None:
        self.db = db
        self._protector = SecretProtector(db.path)
        self._send_override = send_override
        self._lock = threading.RLock()
        self._settings = self._load() or self._validate(defaults)

    def get(self) -> dict[str, Any]:
        with self._lock:
            return self._settings.safe()

    def update(
        self,
        *,
        host: str,
        port: int,
        from_address: str,
        encryption: str,
        username: str,
        password: str | None,
    ) -> dict[str, Any]:
        with self._lock:
            current = self._settings
            candidate = self._validate(MailSettings(
                host=host, port=port, from_address=from_address, encryption=encryption,
                username=(username or "").strip(), password_protected=current.password_protected,
            ))
            if password is not None and password != "__UNCHANGED__":
                candidate = MailSettings(
                    **{**candidate.__dict__, "password_protected": self._protector.protect(password)}
                ) if password else MailSettings(**{**candidate.__dict__, "password_protected": None})
            if candidate.username and candidate.password_protected is None:
                raise ValidationError("設定 SMTP 帳號時必須提供密碼")
            if not candidate.username and candidate.password_protected is not None:
                raise ValidationError("設定 SMTP 密碼時必須提供帳號")

            def job(conn: sqlite3.Connection) -> None:
                conn.execute(
                    "INSERT INTO mail_settings (id, host, port, from_address, encryption, username,"
                    " password_protected, updated_at_ms) VALUES (1, ?, ?, ?, ?, ?, ?, ?)"
                    " ON CONFLICT(id) DO UPDATE SET host=excluded.host, port=excluded.port,"
                    " from_address=excluded.from_address, encryption=excluded.encryption,"
                    " username=excluded.username, password_protected=excluded.password_protected,"
                    " updated_at_ms=excluded.updated_at_ms",
                    (candidate.host, candidate.port, candidate.from_address, candidate.encryption,
                     candidate.username, candidate.password_protected, ids.now_ms()),
                )

            self.db.write(job, label="update_mail_settings")
            self._settings = candidate
            return candidate.safe()

    def send(self, recipient: str, subject: str, text: str) -> None:
        recipient = validate_email(recipient)
        if self._send_override is not None:
            self._send_override(recipient, subject, text)
            return
        with self._lock:
            settings = self._settings
        password = self._protector.unprotect(settings.password_protected) if settings.password_protected else None
        SmtpMailer(
            host=settings.host, port=settings.port, from_address=settings.from_address,
            encryption=settings.encryption, username=settings.username, password=password,
        )(recipient, subject, text)

    def send_test(self, recipient: str) -> None:
        self.send(
            recipient,
            "Tree Agent SMTP 測試信",
            "這是 Tree Agent 的 SMTP 測試信。若您收到此信，郵件設定已可正常寄送。",
        )

    def _load(self) -> MailSettings | None:
        with self.db.read() as conn:
            row = conn.execute("SELECT * FROM mail_settings WHERE id = 1").fetchone()
        if row is None:
            return None
        return MailSettings(
            host=row["host"], port=row["port"], from_address=row["from_address"],
            encryption=row["encryption"], username=row["username"],
            password_protected=row["password_protected"],
        )

    @staticmethod
    def _validate(settings: MailSettings) -> MailSettings:
        if not isinstance(settings.port, int) or isinstance(settings.port, bool) or not 1 <= settings.port <= 65535:
            raise ValidationError("SMTP 連接埠無效")
        if settings.encryption not in ENCRYPTION_MODES:
            raise ValidationError("SMTP 加密模式無效")
        username = settings.username.strip()
        if len(username) > 254 or any(char.isspace() and char not in " \t" for char in username):
            raise ValidationError("SMTP 帳號無效")
        return MailSettings(
            host=validate_host(settings.host), port=settings.port,
            from_address=validate_email(settings.from_address, label="寄件者電子郵件地址"),
            encryption=settings.encryption, username=username,
            password_protected=settings.password_protected,
        )
