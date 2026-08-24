"""Just enough Markdown to make Codex replies readable in a Tk Text widget.

`render()` turns Markdown into a flat list of `(text, tags)` spans, which keeps
the parsing testable without a live widget. `configure_tags()` defines the tags
those spans refer to, and `insert()` writes the spans into a Text widget.

Deliberately not a full Markdown implementation: it covers what Codex actually
emits — ATX headings, bold/italic, inline code, fenced code blocks, bullet and
numbered lists, blockquotes, tables, horizontal rules and links.
"""

from __future__ import annotations

import re
import unicodedata
from typing import Any

Span = tuple[str, tuple[str, ...]]

_HEADING = re.compile(r"^\s{0,3}(#{1,6})\s+(.*)$")
_HR = re.compile(r"^\s{0,3}([-*_])(\s*\1){2,}\s*$")
_QUOTE = re.compile(r"^\s{0,3}>\s?(.*)$")
_BULLET = re.compile(r"^(\s*)([-*+]|\d+[.)])\s+(.*)$")
_FENCE = re.compile(r"^\s*(```|~~~)")

# Order matters: ** before *, __ before _, so the greedy pair wins.
_INLINE = re.compile(
    r"(\*\*.+?\*\*"
    r"|__.+?__"
    r"|`[^`]+`"
    r"|\[[^\]]*\]\([^)\s]+\)"
    r"|\*[^*\n]+\*"
    r"|(?<![A-Za-z0-9_])_[^_\n]+_(?![A-Za-z0-9_]))"
)
_LINK = re.compile(r"^\[([^\]]*)\]\(([^)\s]+)\)$")

MAX_HEADING_LEVEL = 3

# A table wider than this (in monospace columns) is laid out row-by-row instead
# of as a grid: wrapping a wide grid destroys the alignment that makes it a grid.
MAX_TABLE_WIDTH = 100
COLUMN_GAP = 2
# The rule under the header is a divider, not data: a prose column can be 90
# glyphs wide and a rule that long would wrap onto its own line.
MAX_RULE_SEGMENT = 40

# Tags named "<prefix><N>" ask `insert()` to build a hanging indent N monospace
# columns deep, so a wrapped table row lines up under its last column. The pixel
# width is only knowable with a widget in hand, hence the deferred creation.
WRAP_TAG_PREFIX = "md_tablewrap:"

_TABLE_ROW = re.compile(r"^\s*\|.*")
_TABLE_SEPARATOR_CELL = re.compile(r"^:?-+:?$")


def display_width(text: str) -> int:
    """Monospace columns a string occupies — CJK glyphs take two."""
    return sum(
        2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1 for ch in text
    )


def _pad(text: str, width: int) -> str:
    return text + " " * max(0, width - display_width(text))


def _plain(text: str) -> str:
    """Inline markup removed, so cell widths match what is actually drawn."""
    return "".join(chunk for chunk, _ in _inline(text, ()))


def _split_row(line: str) -> list[str]:
    body = line.strip()
    if body.startswith("|"):
        body = body[1:]
    if body.endswith("|"):
        body = body[:-1]
    return [_plain(cell.strip()) for cell in body.split("|")]


def _is_separator_row(cells: list[str]) -> bool:
    filled = [c for c in cells if c]
    return bool(filled) and all(_TABLE_SEPARATOR_CELL.match(c) for c in filled)


def _render_table(lines: list[str]) -> list[Span]:
    """Lay a Markdown table out as an aligned grid, or as blocks if too wide."""
    rows = [_split_row(line) for line in lines]
    rows = [r for r in rows if not _is_separator_row(r)]
    if not rows:
        return []

    columns = max(len(r) for r in rows)
    rows = [r + [""] * (columns - len(r)) for r in rows]
    widths = [max(display_width(r[i]) for r in rows) for i in range(columns)]
    total = sum(widths) + COLUMN_GAP * (columns - 1)

    header, body = rows[0], rows[1:]
    spans: list[Span] = []

    # A two-column table is the shape Codex uses most ("name | description").
    # Its last column can be arbitrarily long, but a hanging indent keeps the
    # wrapped continuation lined up under the column, so the grid still reads.
    grid = columns == 2 or total <= MAX_TABLE_WIDTH
    if grid:
        gap = " " * COLUMN_GAP
        leading = sum(widths[:-1]) + COLUMN_GAP * (columns - 1)
        wrap = f"{WRAP_TAG_PREFIX}{leading}"
        spans.append((gap.join(_pad(c, w) for c, w in zip(header, widths)).rstrip() + "\n",
                      ("md_table_head", wrap)))
        spans.append(
            (gap.join("─" * min(w, MAX_RULE_SEGMENT) for w in widths) + "\n",
             ("md_table_rule",))
        )
        for row in body:
            spans.append((gap.join(_pad(c, w) for c, w in zip(row, widths)).rstrip() + "\n",
                          ("md_table", wrap)))
        return spans

    # Too wide to align: one block per row, each cell labelled by its header.
    for row in body:
        spans.append((row[0] + "\n", ("md_table_key",)))
        for label, cell in zip(header[1:], row[1:]):
            if not cell:
                continue
            prefix = f"{label}: " if label and columns > 2 else ""
            spans.append((prefix + cell + "\n", ("md_table_val",)))
    return spans


def _inline(text: str, base: tuple[str, ...]) -> list[Span]:
    """Split one line into styled spans."""
    out: list[Span] = []
    pos = 0
    for match in _INLINE.finditer(text):
        if match.start() > pos:
            out.append((text[pos : match.start()], base))
        token = match.group(0)
        if token.startswith(("**", "__")):
            out.append((token[2:-2], base + ("md_bold",)))
        elif token.startswith("`"):
            out.append((token[1:-1], base + ("md_code",)))
        elif token.startswith("["):
            link = _LINK.match(token)
            if link:
                label, url = link.group(1), link.group(2)
                out.append((label or url, base + ("md_link",)))
                if label and label != url:
                    out.append((f" ⟨{url}⟩", base + ("md_muted",)))
            else:
                out.append((token, base))
        else:
            out.append((token[1:-1], base + ("md_italic",)))
        pos = match.end()
    if pos < len(text):
        out.append((text[pos:], base))
    return [span for span in out if span[0]]


def render(markdown: str) -> list[Span]:
    """Markdown -> spans. Always ends with exactly one newline."""
    spans: list[Span] = []
    in_fence = False
    pending_blank = False
    wrote_any = False

    lines = markdown.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    index = 0
    while index < len(lines):
        raw = lines[index]
        index += 1

        if _FENCE.match(raw):
            in_fence = not in_fence
            continue

        if in_fence:
            if pending_blank and wrote_any:
                spans.append(("\n", ()))
            pending_blank = False
            spans.append((raw.rstrip() + "\n", ("md_pre",)))
            wrote_any = True
            continue

        line = raw.rstrip()
        if not line.strip():
            pending_blank = wrote_any  # collapse runs of blank lines
            continue

        if pending_blank:
            spans.append(("\n", ()))
            pending_blank = False

        heading = _HEADING.match(line)
        if heading:
            tag = f"md_h{min(len(heading.group(1)), MAX_HEADING_LEVEL)}"
            spans.extend(_inline(heading.group(2), (tag,)))
            spans.append(("\n", (tag,)))
            wrote_any = True
            continue

        if _HR.match(line):
            spans.append(("─" * 32 + "\n", ("md_hr",)))
            wrote_any = True
            continue

        if _TABLE_ROW.match(line):
            block = [line]
            while index < len(lines) and _TABLE_ROW.match(lines[index].rstrip()):
                block.append(lines[index].rstrip())
                index += 1
            rendered = _render_table(block)
            if rendered:
                spans.extend(rendered)
                wrote_any = True
            continue

        quote = _QUOTE.match(line)
        if quote:
            spans.extend(_inline(quote.group(1), ("md_quote",)))
            spans.append(("\n", ("md_quote",)))
            wrote_any = True
            continue

        bullet = _BULLET.match(line)
        if bullet:
            indent, marker, rest = bullet.groups()
            glyph = "•" if marker[0] in "-*+" else marker
            spans.append((" " * len(indent) + glyph + " ", ("md_list",)))
            spans.extend(_inline(rest, ("md_list",)))
            spans.append(("\n", ("md_list",)))
            wrote_any = True
            continue

        spans.extend(_inline(line, ()))
        spans.append(("\n", ()))
        wrote_any = True

    if not spans:
        return [("\n", ())]
    return spans


def configure_tags(text: Any, ui_font: str, mono_font: str, colors: dict[str, str]) -> None:
    """Define the Markdown tags.

    Call this *after* the per-role body tags: Tk resolves conflicts by tag
    creation order, so these must be created later to win on font and margins.
    """
    left = 12
    text.tag_configure("md_h1", font=(ui_font, 13, "bold"), spacing1=10, spacing3=3,
                       lmargin1=left, lmargin2=left)
    text.tag_configure("md_h2", font=(ui_font, 11, "bold"), spacing1=9, spacing3=2,
                       lmargin1=left, lmargin2=left)
    text.tag_configure("md_h3", font=(ui_font, 10, "bold"), spacing1=7, spacing3=2,
                       lmargin1=left, lmargin2=left)
    text.tag_configure("md_bold", font=(ui_font, 10, "bold"))
    text.tag_configure("md_italic", font=(ui_font, 10, "italic"))
    text.tag_configure("md_code", font=(mono_font, 9), background=colors["tool_bg"])
    text.tag_configure("md_pre", font=(mono_font, 9), background=colors["tool_bg"],
                       lmargin1=left + 10, lmargin2=left + 10, spacing1=0, spacing3=0)
    text.tag_configure("md_list", lmargin1=left + 8, lmargin2=left + 22)
    text.tag_configure("md_quote", foreground=colors["muted"], lmargin1=left + 12,
                       lmargin2=left + 12)
    text.tag_configure("md_table", font=(mono_font, 9), lmargin1=left, lmargin2=left)
    text.tag_configure("md_table_head", font=(mono_font, 9, "bold"),
                       lmargin1=left, lmargin2=left, spacing1=6)
    text.tag_configure("md_table_rule", font=(mono_font, 9), foreground=colors["border"],
                       lmargin1=left, lmargin2=left)
    # Row-per-block fallback for tables too wide to align.
    text.tag_configure("md_table_key", font=(mono_font, 9, "bold"),
                       lmargin1=left, lmargin2=left, spacing1=6)
    text.tag_configure("md_table_val", lmargin1=left + 16, lmargin2=left + 16)
    text.tag_configure("md_hr", foreground=colors["border"], lmargin1=left)
    text.tag_configure("md_link", foreground=colors["accent"], underline=True)
    text.tag_configure("md_muted", foreground=colors["muted"], font=(ui_font, 9))


def ensure_wrap_tag(text: Any, tag: str) -> None:
    """Create a hanging-indent tag for wrapped table rows, once per depth."""
    if tag in text.tag_names():
        return
    try:
        columns = int(tag[len(WRAP_TAG_PREFIX):])
    except ValueError:
        return
    from tkinter import font as tkfont

    spec = text.tag_cget("md_table", "font") or text.cget("font")
    try:
        char = tkfont.Font(root=text, font=spec).measure("0")
    except Exception:
        char = 7
    try:
        left = int(text.tag_cget("md_table", "lmargin1") or 0)
    except (ValueError, TypeError):
        left = 0
    text.tag_configure(tag, lmargin1=left, lmargin2=left + columns * char)


def insert(text: Any, markdown: str, base_tags: tuple[str, ...] = ()) -> None:
    """Write rendered Markdown at the Text widget's end."""
    for chunk, tags in render(markdown):
        for tag in tags:
            if tag.startswith(WRAP_TAG_PREFIX):
                ensure_wrap_tag(text, tag)
        text.insert("end", chunk, base_tags + tags)
