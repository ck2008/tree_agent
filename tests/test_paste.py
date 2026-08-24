"""Pasting images: DIB -> PNG conversion, thumbnails, and right-click paste.

Why PNG and not BMP: Tk's PhotoImage decodes PNG and GIF but not BMP, so a BMP
attachment could never show a preview. PNG is also far smaller — a 200x80
screenshot measured 301 bytes as PNG against ~28 KB as BMP. `codex exec -i`
accepts both (verified on 0.149), so the choice is ours.
"""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import struct, subprocess, tempfile, tkinter as tk
from tree_agent import clipboard_image as ci
from tree_agent.app import TreeAgentApp, THUMBNAIL_HEIGHT


def build_dib(width, height, bpp=24, compression=0, pixels=None, palette=b"", clr_used=0):
    """A DIB exactly as the clipboard hands one over."""
    header = struct.pack("<IiiHHIIiiII", 40, width, height, 1, bpp, compression,
                         0, 96, 96, clr_used, 0)
    return header + palette + (pixels or b"")


def stride(width, bpp):
    return ((width * bpp + 31) // 32) * 4


def png_size(blob):
    assert blob[:8] == b"\x89PNG\r\n\x1a\n", "not a PNG"
    w, h, depth, ctype = struct.unpack(">IIBB", blob[16:26])
    return w, h, depth, ctype


# ---- 24bpp bottom-up: the default, and the row order that is easy to get wrong ----
# two rows: the FIRST row of pixel data is the BOTTOM of the image
row_bottom = bytes([0, 0, 255]) + b"\x00" * (stride(1, 24) - 3)      # BGR red
row_top = bytes([255, 0, 0]) + b"\x00" * (stride(1, 24) - 3)         # BGR blue
png = ci.dib_to_png(build_dib(1, 2, 24, pixels=row_bottom + row_top))
assert png_size(png) == (1, 2, 8, 2), png_size(png)
print("24bpp bottom-up converts OK")

# ---- a negative height means the rows are already top-down ----
png_td = ci.dib_to_png(build_dib(1, -2, 24, pixels=row_top + row_bottom))
assert png_td is not None
assert png_td == png, "top-down and bottom-up of the same image must agree"
print("negative height (top-down rows) handled OK")

# ---- 32bpp BI_BITFIELDS, which is what Windows screenshots usually are ----
masks = struct.pack("<III", 0x00FF0000, 0x0000FF00, 0x000000FF)
px32 = bytes([90, 160, 20, 0]) * 2          # BGRA, alpha deliberately zero
png32 = ci.dib_to_png(build_dib(2, 1, 32, compression=3, pixels=px32, palette=masks))
assert png_size(png32) == (2, 1, 8, 2), png_size(png32)
print("32bpp BI_BITFIELDS converts, alpha dropped OK")

# ---- 8bpp with a palette ----
pal = b"".join(bytes([i, i, i, 0]) for i in range(256))
px8 = bytes([5]) + b"\x00" * (stride(1, 8) - 1)
png8 = ci.dib_to_png(build_dib(1, 1, 8, pixels=px8, palette=pal, clr_used=256))
assert png_size(png8) == (1, 1, 8, 2), png_size(png8)
print("8bpp palette converts OK")

# ---- layouts we refuse, so the BMP fallback takes over instead of crashing ----
assert ci.dib_to_png(b"short") is None
assert ci.dib_to_png(build_dib(1, 1, 16, pixels=b"\x00" * 4)) is None, "16bpp unsupported"
assert ci.dib_to_png(build_dib(1, 1, 24, compression=1, pixels=b"\x00" * 4)) is None, "RLE"
assert ci.dib_to_png(build_dib(0, 1, 24, pixels=b"")) is None
assert ci.dib_to_png(build_dib(4, 4, 24, pixels=b"\x00" * 4)) is None, "truncated pixels"
print("unsupported layouts decline cleanly OK")

# ---- the real clipboard, when this machine can populate it ----
def set_clipboard(width, height, r, g, b):
    script = (
        "Add-Type -AssemblyName System.Windows.Forms,System.Drawing;"
        f"$m=New-Object System.Drawing.Bitmap {width},{height};"
        "$g=[System.Drawing.Graphics]::FromImage($m);"
        f"$g.Clear([System.Drawing.Color]::FromArgb({r},{g},{b}));"
        "$g.FillRectangle([System.Drawing.Brushes]::White,0,0,2,2);$g.Dispose();"
        "[System.Windows.Forms.Clipboard]::SetImage($m);$m.Dispose()"
    )
    try:
        return subprocess.run(["powershell", "-NoProfile", "-Command", script],
                              capture_output=True, timeout=120).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


root = tk.Tk()
have_clipboard = set_clipboard(200, 80, 20, 160, 90)
if have_clipboard:
    out = tempfile.mkdtemp()
    saved = ci.save_clipboard_image(out, "shot")
    assert saved and saved.endswith(".png"), saved
    img = tk.PhotoImage(master=root, file=saved)
    assert (img.width(), img.height()) == (200, 80), (img.width(), img.height())
    # white block is at the TOP-left, proving the bottom-up flip is right
    assert img.get(0, 0)[:3] == (255, 255, 255), img.get(0, 0)
    assert img.get(100, 40)[:3] == (20, 160, 90), img.get(100, 40)
    assert os.path.getsize(saved) < 20000, "PNG should be far smaller than the BMP"
    print(f"clipboard -> PNG OK ({os.path.getsize(saved)} bytes, colours and rows correct)")
else:
    print("cannot drive the clipboard here; synthetic DIBs still cover the conversion")

# ---- thumbnails and the attachment cards ----
home = tempfile.mkdtemp()
app = TreeAgentApp(root, home=home, single_instance=False)
conv = app.ws.projects[0]["children"][0]
app.refresh_tree(); app._select(conv["id"]); root.update()
view = app.conv_view

tmp = tempfile.mkdtemp()
def write_png(name, w, h):
    path = os.path.join(tmp, name)
    with open(path, "wb") as fh:
        fh.write(ci._png(w, h, [bytes((200, 30, 30)) * w for _ in range(h)]))
    return path

wide = write_png("a-really-long-screenshot-file-name.png", 400, 200)
tiny = write_png("tiny.png", 16, 16)
opaque = os.path.join(tmp, "photo.jpg")
open(opaque, "wb").write(b"jpeg bytes Tk cannot decode")

view.add_attachments([wide, tiny, opaque])
root.update()
assert view.attach_bar.winfo_ismapped()
assert len(view.attach_bar.winfo_children()) == 3, "one card per attachment"
# only the PNGs get a preview; the undecodable file still gets a card
assert len(view._thumbnails) == 2, len(view._thumbnails)
for thumb in view._thumbnails:
    assert thumb.height() <= THUMBNAIL_HEIGHT, thumb.height()
assert any(t.width() == 80 and t.height() == 40 for t in view._thumbnails), \
    [(t.width(), t.height()) for t in view._thumbnails]
print("thumbnail cards built, oversized preview downsampled OK")

labels = []
def collect(widget):
    for child in widget.winfo_children():
        if isinstance(child, tk.Label):
            labels.append(child.cget("text"))
        collect(child)
collect(view.attach_bar)
assert any("…" in text for text in labels), labels
assert "🖼" in labels, "undecodable images fall back to a glyph"
print("long names elided, fallback glyph shown OK")

# a removal rebuilds the strip and releases the reference that kept it alive
view.remove_attachment(wide)
root.update()
assert len(view.attach_bar.winfo_children()) == 2
assert len(view._thumbnails) == 1
print("removal rebuilds the strip OK")

# ---- right-click paste follows the clipboard ----
if have_clipboard:
    set_clipboard(40, 40, 10, 20, 30)
    label, is_image = view.paste_entry()
    assert is_image and label == "貼上圖片", (label, is_image)
    before = len(view.current_attachments())
    assert view.paste_image() is True
    root.update()
    assert len(view.current_attachments()) == before + 1
    assert view.current_attachments()[-1].endswith(".png")
    # Ctrl+V consumes the event when there is an image to take
    set_clipboard(40, 40, 10, 20, 30)
    assert view._on_paste() == "break"
    print("right-click and Ctrl+V both attach the clipboard image OK")

# The text case is driven through the detection helpers rather than by putting
# text on the real clipboard: `clipboard_append` would make this process the
# clipboard OWNER with delayed rendering, and a test that drives Tk with
# update() instead of mainloop() has nothing to service Windows' render request
# at teardown — the process then hangs on exit holding an open window.
real_has_image, real_files = ci.has_image, ci.clipboard_files
ci.has_image = lambda: False
ci.clipboard_files = lambda: []
try:
    label, is_image = view.paste_entry()
    assert not is_image and label == "貼上", (label, is_image)
    assert view._on_paste() is None, "plain text must fall through to Tk's text paste"
    before = len(view.current_attachments())
    assert view.paste_image() is False
    assert len(view.current_attachments()) == before
    assert "沒有圖片" in app.status.cget("text"), app.status.cget("text")
finally:
    ci.has_image, ci.clipboard_files = real_has_image, real_files
print("text on the clipboard still pastes as text OK")

# Explorer-style file paths on the clipboard attach as-is
ci.has_image = lambda: False
ci.clipboard_files = lambda: [tiny]
try:
    label, is_image = view.paste_entry()
    assert is_image and "1 個檔案" in label, label
    view.attachments.pop(conv["id"], None)
    assert view.paste_image() is True
    assert view.current_attachments() == [tiny], view.current_attachments()
finally:
    ci.has_image, ci.clipboard_files = real_has_image, real_files
print("copied image files attach without re-encoding OK")

app.on_close()
print("\nALL PASTE TESTS PASSED")
