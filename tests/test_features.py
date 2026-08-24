"""Image attachments, the review action, archive-on-delete, and usage totals.

Measured facts these rely on (codex 0.149):
  * `-i` on `exec` is variadic, so `-i a.png "prompt"` eats the prompt as a
    second file — the flag has to be repeated once per file.
  * `-i` works either side of the `resume` sub-command; we keep it with the
    shared options so new / resume / fork share one code path.
  * `review --uncommitted` refuses a PROMPT: "the argument '--uncommitted'
    cannot be used with '[PROMPT]'".
  * `-i` accepts both PNG and BMP, so the clipboard DIB is converted to PNG
    (Tk can preview PNG, and it is much smaller), with BMP as the fallback
    for pixel layouts the converter declines. test_paste covers that path.
"""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import struct, subprocess, tempfile, tkinter as tk
import tkinter.messagebox as mb
from tree_agent import clipboard_image as ci
from tree_agent import codex_runner as cr
from tree_agent import store
from tree_agent.app import TreeAgentApp

# ============================ images ============================

cmd = cr.Turn("p", ".", lambda e: None, images=["a.png", "b.bmp"]).build_command()
assert cmd.count("-i") == 2, cmd
assert cmd[cmd.index("-i") + 1] == "a.png"
assert cmd[-1] == "-", cmd
for extra in ({"thread_id": "TID"}, {"fork_from": "SRC"}):
    c = cr.Turn("p", ".", lambda e: None, images=["a.png"], **extra).build_command()
    sub = "resume" if "thread_id" in extra else "fork"
    assert c.index("-i") < c.index(sub), c
assert "-i" not in cr.Turn("p", ".", lambda e: None).build_command()
print("image flags repeat once per file and precede the sub-command OK")

# DIB header arithmetic, for the pixel layouts Windows actually hands over
def dib(header=40, bpp=24, compression=0, clr_used=0, palette_bytes=0, extra=0):
    return (struct.pack("<IiiHHIIiiII", header, 4, 4, 1, bpp, compression,
                        64, 96, 96, clr_used, 0)
            + b"\x00" * (palette_bytes + extra))

assert ci._pixel_offset(dib()) == 40                                  # 24bpp, no palette
assert ci._pixel_offset(dib(bpp=8, clr_used=256, palette_bytes=1024)) == 1064
assert ci._pixel_offset(dib(bpp=8, clr_used=0, palette_bytes=1024)) == 40 + 256 * 4
assert ci._pixel_offset(dib(bpp=32, compression=3, palette_bytes=12)) == 52  # BI_BITFIELDS
print("DIB pixel offsets computed for 8 / 24 / 32bpp OK")

assert ci.looks_like_image("A.PNG") and ci.looks_like_image("b.bmp")
assert not ci.looks_like_image("notes.txt")

# The clipboard capture path — format choice, PNG headers, colours, row order —
# is covered in test_paste. What belongs here is the BMP fallback, which a real
# clipboard never reaches because its DIBs always decode.
tmp_bmp = tempfile.mkdtemp()
raw = struct.pack("<IiiHHIIiiII", 40, 2, 2, 1, 16, 0, 0, 96, 96, 0, 0) + bytes(16)
assert ci.dib_to_png(raw) is None, "16bpp is not decoded, so BMP must take over"
path = ci._save_bmp(os.path.join(tmp_bmp, "fallback.bmp"), raw)
size = os.path.getsize(path)
with open(path, "rb") as fh:
    sig, declared, _, _, offset = struct.unpack("<2sIHHI", fh.read(14))
assert sig == b"BM" and declared == size, (sig, declared, size)
assert offset == 14 + 40, offset
print("BMP fallback writes a valid file for DIBs we cannot decode OK")

# ============================ review ============================

cmd = cr.Turn("p", ".", lambda e: None, review=cr.REVIEW_UNCOMMITTED).build_command()
assert cmd[-2:] == ["review", "--uncommitted"], cmd
assert "-" != cmd[-1], "--uncommitted must not be given a PROMPT placeholder"
cmd = cr.Turn("p", ".", lambda e: None, review=cr.REVIEW_CUSTOM).build_command()
assert cmd[-2:] == ["review", "-"], cmd
# review replaces resume/fork rather than stacking with them
cmd = cr.Turn("p", ".", lambda e: None, review=cr.REVIEW_UNCOMMITTED,
              thread_id="TID", fork_from="SRC").build_command()
assert "resume" not in cmd and "fork" not in cmd, cmd
# shared options still land before the sub-command
cmd = cr.Turn("p", ".", lambda e: None, review=cr.REVIEW_UNCOMMITTED,
              sandbox="read-only", images=["a.png"]).build_command()
assert cmd.index("-s") < cmd.index("review") and cmd.index("-i") < cmd.index("review"), cmd
print("review command building OK")

# ============================ usage ============================

home = tempfile.mkdtemp()
ws = store.Workspace(home)
proj = ws.projects[0]
sub = ws.add_project(proj["id"], "子專案")
a = ws.add_conversation(proj["id"], "A")
b = ws.add_conversation(sub["id"], "B")

assert ws.usage_of(a["id"]) == {}
ws.add_usage(a["id"], {"input_tokens": 100, "cached_input_tokens": 40,
                       "output_tokens": 10, "reasoning_output_tokens": 5})
total = ws.add_usage(a["id"], {"input_tokens": 200, "output_tokens": 20})
assert total == {"turns": 2, "input_tokens": 300, "cached_input_tokens": 40,
                 "output_tokens": 30, "reasoning_output_tokens": 5}, total
ws.add_usage(b["id"], {"input_tokens": 7, "output_tokens": 3})

# a project sums its whole subtree, including nested projects
roll = ws.usage_of(proj["id"])
assert roll["turns"] == 3 and roll["input_tokens"] == 307 and roll["output_tokens"] == 33, roll
assert ws.usage_of(sub["id"])["input_tokens"] == 7
# junk in the event must not poison the totals
ws.add_usage(a["id"], {"input_tokens": None, "output_tokens": "lots"})
assert ws.usage_of(a["id"])["input_tokens"] == 300
assert ws.usage_of(a["id"])["turns"] == 3
ws.save()
assert store.Workspace(home).usage_of(proj["id"])["input_tokens"] == 307
print("usage accumulates per conversation and rolls up per project OK")

# unique_name keeps repeated actions from colliding
assert ws.unique_name(proj, "程式碼審查") == "程式碼審查"
ws.add_conversation(proj["id"], "程式碼審查")
assert ws.unique_name(ws.find(proj["id"]), "程式碼審查") == "程式碼審查 2"
print("unique_name avoids sibling collisions OK")

# ============================ the UI ============================

home2 = tempfile.mkdtemp()
root = tk.Tk()
app = TreeAgentApp(root, home=home2, single_instance=False)
top = app.ws.projects[0]
conv = top["children"][0]
app.refresh_tree()
app._select(conv["id"])
root.update()
view = app.conv_view

# attachments: hidden until something is attached, per-conversation, removable
assert not view.attach_bar.winfo_ismapped()
tmp = tempfile.mkdtemp()
img1 = os.path.join(tmp, "one.png"); open(img1, "wb").write(b"x")
img2 = os.path.join(tmp, "two.bmp"); open(img2, "wb").write(b"x")
view.add_attachments([img1, img2, img1, os.path.join(tmp, "missing.png")])
root.update()
assert view.current_attachments() == [img1, img2], view.current_attachments()
assert view.attach_bar.winfo_ismapped()
view.remove_attachment(img1)
root.update()
assert view.current_attachments() == [img2]
print("attachment strip adds, dedupes, skips missing files and removes OK")

other = app.ws.add_conversation(top["id"], "另一個對話")
app.refresh_tree()
app._select(other["id"]); root.update()
assert view.current_attachments() == [], "attachments must not follow you across conversations"
assert not view.attach_bar.winfo_ismapped()
app._select(conv["id"]); root.update()
assert view.current_attachments() == [img2], "and must come back with their own conversation"
print("attachments are per-conversation OK")

# sending passes the images through and clears the strip
sent = {}
app.send = lambda cid, prompt, images=None, review=None: sent.update(
    conv=cid, prompt=prompt, images=images, review=review)
view.input.insert("1.0", "看這張圖")
view.on_send()
root.update()
assert sent["images"] == [img2], sent
assert sent["prompt"] == "看這張圖"
assert view.current_attachments() == []
assert not view.attach_bar.winfo_ismapped()
print("send forwards the attachments then clears them OK")

# an attachment with no text still sends
view.add_attachments([img1])
sent.clear()
view.on_send()
assert sent.get("images") == [img1], sent
assert sent.get("prompt"), "an image-only message needs some prompt text"
print("image-only send works OK")

app.on_close()
print("\nALL FEATURE TESTS PASSED")
