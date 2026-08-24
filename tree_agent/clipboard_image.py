"""Pull a bitmap off the Windows clipboard and save it as a file.

Tkinter cannot read image formats from the clipboard, and the project takes no
third-party dependencies, so this talks to the Win32 clipboard through ctypes.

The clipboard hands over a DIB (device-independent bitmap), which is precisely a
BMP file minus its 14-byte file header — so writing it out is a header plus a
copy, with no pixel-format handling at all. `codex exec -i` accepts BMP
(verified against 0.149), which is why this takes the simple route rather than
re-encoding to PNG.
"""

from __future__ import annotations

import os
import struct
import time
import zlib
from typing import Any

BI_RGB = 0
BI_BITFIELDS = 3

CF_DIB = 8
CF_DIBV5 = 17
CF_HDROP = 15

_IMAGE_SUFFIXES = (".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp")

_BITMAPFILEHEADER = 14
_BITMAPINFOHEADER = 40


_libs: tuple[Any, Any, Any] | None = None
_libs_loaded = False


def _win32() -> tuple[Any, Any, Any] | None:
    """(user32, kernel32, shell32) with prototypes declared, or None.

    Declaring restypes is not optional: these functions return handles and
    pointers, and ctypes defaults to `c_int`, which silently truncates them to
    32 bits on 64-bit Python — the handle then looks valid but points nowhere.
    """
    global _libs, _libs_loaded
    if _libs_loaded:
        return _libs
    _libs_loaded = True
    if os.name != "nt":
        return None
    try:
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.WinDLL("user32", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        shell32 = ctypes.WinDLL("shell32", use_last_error=True)

        user32.OpenClipboard.argtypes = [wintypes.HWND]
        user32.OpenClipboard.restype = wintypes.BOOL
        user32.CloseClipboard.argtypes = []
        user32.CloseClipboard.restype = wintypes.BOOL
        user32.IsClipboardFormatAvailable.argtypes = [wintypes.UINT]
        user32.IsClipboardFormatAvailable.restype = wintypes.BOOL
        user32.GetClipboardData.argtypes = [wintypes.UINT]
        user32.GetClipboardData.restype = wintypes.HANDLE

        kernel32.GlobalLock.argtypes = [wintypes.HGLOBAL]
        kernel32.GlobalLock.restype = ctypes.c_void_p
        kernel32.GlobalUnlock.argtypes = [wintypes.HGLOBAL]
        kernel32.GlobalUnlock.restype = wintypes.BOOL
        kernel32.GlobalSize.argtypes = [wintypes.HGLOBAL]
        kernel32.GlobalSize.restype = ctypes.c_size_t

        shell32.DragQueryFileW.argtypes = [
            wintypes.HANDLE, wintypes.UINT, wintypes.LPWSTR, wintypes.UINT
        ]
        shell32.DragQueryFileW.restype = wintypes.UINT

        _libs = (user32, kernel32, shell32)
    except Exception:
        _libs = None
    return _libs


def has_image() -> bool:
    """True when the clipboard currently holds a bitmap."""
    libs = _win32()
    if libs is None:
        return False
    user32 = libs[0]
    return bool(user32.IsClipboardFormatAvailable(CF_DIB) or
                user32.IsClipboardFormatAvailable(CF_DIBV5))


def clipboard_files() -> list[str]:
    """Image file paths copied in Explorer (CF_HDROP), if any."""
    libs = _win32()
    if libs is None:
        return []
    import ctypes

    user32, _, shell32 = libs
    if not user32.IsClipboardFormatAvailable(CF_HDROP):
        return []
    if not user32.OpenClipboard(None):
        return []
    try:
        handle = user32.GetClipboardData(CF_HDROP)
        if not handle:
            return []
        count = shell32.DragQueryFileW(handle, 0xFFFFFFFF, None, 0)
        paths = []
        for i in range(count):
            length = shell32.DragQueryFileW(handle, i, None, 0)
            buffer = ctypes.create_unicode_buffer(length + 1)
            shell32.DragQueryFileW(handle, i, buffer, length + 1)
            paths.append(buffer.value)
        return [p for p in paths if p.lower().endswith(_IMAGE_SUFFIXES)]
    finally:
        user32.CloseClipboard()


def _dib_bytes() -> bytes | None:
    libs = _win32()
    if libs is None:
        return None
    import ctypes

    user32, kernel32, _ = libs
    fmt = CF_DIB if user32.IsClipboardFormatAvailable(CF_DIB) else CF_DIBV5
    if not user32.IsClipboardFormatAvailable(fmt):
        return None
    # Another process may hold the clipboard for a moment; a couple of retries
    # is the documented way to cope.
    for attempt in range(5):
        if user32.OpenClipboard(None):
            break
        time.sleep(0.02 * (attempt + 1))
    else:
        return None
    try:
        handle = user32.GetClipboardData(fmt)
        if not handle:
            return None
        size = kernel32.GlobalSize(handle)
        pointer = kernel32.GlobalLock(handle)
        if not pointer or not size:
            return None
        try:
            return ctypes.string_at(pointer, size)
        finally:
            kernel32.GlobalUnlock(handle)
    finally:
        user32.CloseClipboard()


def _pixel_offset(dib: bytes) -> int:
    """Where the pixel array starts inside the DIB."""
    header_size = struct.unpack_from("<I", dib, 0)[0]
    bit_count = struct.unpack_from("<H", dib, 14)[0] if header_size >= 16 else 0
    compression = struct.unpack_from("<I", dib, 16)[0] if header_size >= 20 else 0
    clr_used = struct.unpack_from("<I", dib, 32)[0] if header_size >= 36 else 0

    palette = 0
    if bit_count <= 8:
        palette = (clr_used or (1 << bit_count)) * 4
    elif compression == 3:  # BI_BITFIELDS stores three colour masks
        palette = 12
    return header_size + palette


def _png(width: int, height: int, rgb_rows: list[bytes]) -> bytes:
    """Encode 8-bit truecolour RGB rows as a PNG (zlib is in the stdlib)."""

    def chunk(tag: bytes, data: bytes) -> bytes:
        body = tag + data
        return (struct.pack(">I", len(data)) + body
                + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF))

    raw = b"".join(b"\x00" + row for row in rgb_rows)   # filter type 0 per row
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr)
            + chunk(b"IDAT", zlib.compress(raw, 6)) + chunk(b"IEND", b""))


def dib_to_png(dib: bytes) -> bytes | None:
    """Convert a clipboard DIB to PNG, or None for a layout we do not handle.

    PNG rather than BMP because Tk's PhotoImage can display PNG (so the
    attachment can show a real thumbnail) and it is far smaller — a 120x60
    screenshot measured 28 KB as BMP against well under 1 KB as PNG.

    Alpha is dropped on purpose: `Clipboard.SetImage` frequently hands over
    32bpp pixels with a zero alpha channel, and honouring that would produce a
    fully transparent image.
    """
    if len(dib) < _BITMAPINFOHEADER:
        return None
    header_size = struct.unpack_from("<I", dib, 0)[0]
    width, height = struct.unpack_from("<ii", dib, 4)
    bit_count = struct.unpack_from("<H", dib, 14)[0]
    compression = struct.unpack_from("<I", dib, 16)[0]
    clr_used = struct.unpack_from("<I", dib, 32)[0]

    if width <= 0 or height == 0 or bit_count not in (8, 24, 32):
        return None
    if compression not in (BI_RGB, BI_BITFIELDS):
        return None            # RLE or JPEG/PNG-in-BMP: not worth handling

    top_down = height < 0
    rows_count = abs(height)
    offset = _pixel_offset(dib)

    palette: list[tuple[int, int, int]] = []
    if bit_count == 8:
        count = clr_used or 256
        base = header_size
        if len(dib) < base + count * 4:
            return None
        for i in range(count):
            b, g, r, _ = dib[base + i * 4: base + i * 4 + 4]
            palette.append((r, g, b))

    stride = ((width * bit_count + 31) // 32) * 4
    if len(dib) < offset + stride * rows_count:
        return None

    rows: list[bytes] = []
    for y in range(rows_count):
        start = offset + y * stride
        line = dib[start: start + stride]
        out = bytearray(width * 3)
        if bit_count == 8:
            for x in range(width):
                r, g, b = palette[line[x]] if line[x] < len(palette) else (0, 0, 0)
                out[x * 3: x * 3 + 3] = bytes((r, g, b))
        else:
            step = bit_count // 8
            for x in range(width):
                b, g, r = line[x * step], line[x * step + 1], line[x * step + 2]
                out[x * 3: x * 3 + 3] = bytes((r, g, b))
        rows.append(bytes(out))

    if not top_down:
        rows.reverse()          # DIB rows run bottom-up unless height is negative
    return _png(width, rows_count, rows)


def save_clipboard_image(directory: str, stem: str = "paste") -> str | None:
    """Write the clipboard bitmap into `directory`; return its path.

    PNG when the DIB can be decoded, otherwise BMP — which is a header plus a
    copy and therefore always works. `codex exec -i` accepts both.
    """
    dib = _dib_bytes()
    if not dib or len(dib) < _BITMAPINFOHEADER:
        return None
    os.makedirs(directory, exist_ok=True)
    base = os.path.join(directory, f"{stem}-{int(time.time() * 1000)}")

    png = None
    try:
        png = dib_to_png(dib)
    except (struct.error, IndexError, ValueError, MemoryError):
        png = None
    if png:
        path = base + ".png"
        with open(path, "wb") as fh:
            fh.write(png)
        return path
    return _save_bmp(base + ".bmp", dib)


def _save_bmp(path: str, dib: bytes) -> str:
    """A DIB is a BMP without its 14-byte file header, so prepend one."""
    offset = _BITMAPFILEHEADER + _pixel_offset(dib)
    header = struct.pack("<2sIHHI", b"BM", _BITMAPFILEHEADER + len(dib), 0, 0, offset)
    with open(path, "wb") as fh:
        fh.write(header + dib)
    return path


def looks_like_image(path: str) -> bool:
    return path.lower().endswith(_IMAGE_SUFFIXES)
