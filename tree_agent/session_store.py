"""Per-Windows-user storage for the optional remembered login session.

Only opaque, server-issued bearer tokens go through this module.  Passwords
must never be persisted by the desktop client.  On Windows the bytes are
protected with DPAPI for the current Windows account before they reach disk;
copying the file to another account or machine cannot reveal the token.
"""

from __future__ import annotations

import base64
import ctypes
from ctypes import wintypes
import os
from pathlib import Path


class _DataBlob(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_byte))]


_CRYPTPROTECT_UI_FORBIDDEN = 0x1


def _blob(value: bytes) -> tuple[_DataBlob, ctypes.Array[ctypes.c_char]]:
    buffer = ctypes.create_string_buffer(value)
    return _DataBlob(len(value), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_byte))), buffer


def _protect(value: bytes) -> bytes:
    if os.name != "nt":
        raise RuntimeError("記住登入僅支援 Windows 的安全憑證儲存")
    source, _source_buffer = _blob(value)
    output = _DataBlob()
    if not ctypes.windll.crypt32.CryptProtectData(
        ctypes.byref(source), "Tree Agent session", None, None, None,
        _CRYPTPROTECT_UI_FORBIDDEN, ctypes.byref(output),
    ):
        raise ctypes.WinError()
    try:
        return ctypes.string_at(output.pbData, output.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(output.pbData)


def _unprotect(value: bytes) -> bytes:
    if os.name != "nt":
        raise RuntimeError("記住登入僅支援 Windows 的安全憑證儲存")
    source, _source_buffer = _blob(value)
    output = _DataBlob()
    if not ctypes.windll.crypt32.CryptUnprotectData(
        ctypes.byref(source), None, None, None, None, _CRYPTPROTECT_UI_FORBIDDEN,
        ctypes.byref(output),
    ):
        raise ctypes.WinError()
    try:
        return ctypes.string_at(output.pbData, output.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(output.pbData)


class SessionStore:
    """Read and replace a single encrypted token for one desktop profile."""

    def __init__(self, home: str) -> None:
        self.path = Path(home) / "remembered-session.bin"

    def load(self) -> str | None:
        try:
            encoded = self.path.read_bytes()
            return _unprotect(base64.b64decode(encoded, validate=True)).decode("utf-8")
        except (OSError, ValueError, UnicodeDecodeError):
            self.clear()
            return None
        except Exception:
            # A token encrypted for a former Windows profile is deliberately
            # unusable. Treat it as logged out without exposing DPAPI details.
            self.clear()
            return None

    def save(self, token: str) -> None:
        if not token:
            self.clear()
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        protected = base64.b64encode(_protect(token.encode("utf-8")))
        temporary = self.path.with_suffix(".tmp")
        temporary.write_bytes(protected)
        os.replace(temporary, self.path)

    def clear(self) -> None:
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass
