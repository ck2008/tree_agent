"""Markdown -> span rendering, and that it lands in a real Text widget."""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import tkinter as tk
from tree_agent import richtext
from tree_agent.app import COLORS


def tags_for(spans, needle):
    """Tags applied to the first span whose text contains `needle`."""
    for text, tags in spans:
        if needle in text:
            return tags
    raise AssertionError(f"{needle!r} not found in {spans}")


def plain(spans):
    return "".join(t for t, _ in spans)


# ---- headings ----
s = richtext.render("### 一、需求理解")
assert tags_for(s, "一、需求理解") == ("md_h3",), s
assert "#" not in plain(s), plain(s)
assert richtext.render("# Top")[0][1] == ("md_h1",)
assert richtext.render("## Mid")[0][1] == ("md_h2",)
assert richtext.render("###### Deep")[0][1] == ("md_h3",), "levels past 3 collapse"
print("headings OK")

# ---- inline emphasis and code ----
s = richtext.render("這是 **粗體** 與 `code` 和 *斜體*。")
assert tags_for(s, "粗體") == ("md_bold",)
assert tags_for(s, "code") == ("md_code",)
assert tags_for(s, "斜體") == ("md_italic",)
assert "**" not in plain(s) and "`" not in plain(s), plain(s)
print("inline emphasis OK")

# underscores inside identifiers must not become italics
s = richtext.render("欄位 redirect_uri 與 home_new_aspx 不變")
assert plain(s).strip() == "欄位 redirect_uri 與 home_new_aspx 不變", plain(s)
assert all(tags == () for _, tags in s), s
print("snake_case survives OK")

# ---- fenced code block ----
s = richtext.render('文字\n```xml\n<add key="a" value="b" />\n```\n之後')
assert "```" not in plain(s)
assert "xml" not in plain(s), "the fence's language label is not content"
assert tags_for(s, "<add key=") == ("md_pre",)
assert tags_for(s, "之後") == ()
print("fenced code OK")

# blank lines inside a fence are preserved verbatim
s = richtext.render("```\na\n\nb\n```")
pre = [t for t, tags in s if tags == ("md_pre",)]
assert pre == ["a\n", "\n", "b\n"], pre
print("fence keeps its own blank lines OK")

# ---- lists ----
s = richtext.render("- 第一\n- 第二\n  - 巢狀")
assert tags_for(s, "第一") == ("md_list",)
assert "• " in plain(s)
assert "  • " in plain(s), "indent is preserved for nested items"
s = richtext.render("1. 單純說明\n2. 也一併分析")
assert "1. " in plain(s) and "2. " in plain(s), plain(s)
assert tags_for(s, "單純說明") == ("md_list",)
print("lists OK")

# ---- links ----
s = richtext.render("見 [Web.config](file:///c/Web.config) 第 6 行")
assert tags_for(s, "Web.config") == ("md_link",)
assert tags_for(s, "⟨file:///c/Web.config⟩") == ("md_muted",)
assert "](" not in plain(s)
print("links OK")

# ---- quotes, tables, rules ----
assert tags_for(richtext.render("> 注意"), "注意") == ("md_quote",)
assert richtext.render("---")[0][1] == ("md_hr",)
assert richtext.render("- - -")[0][1] == ("md_hr",), "spaced rule"
print("quotes / tables / rules OK")

# ---- whitespace hygiene ----
s = richtext.render("A\n\n\n\nB")
assert plain(s) == "A\n\nB\n", repr(plain(s))
assert plain(richtext.render("\n\n  \n")) == "\n", repr(plain(richtext.render("\n\n  \n")))
assert plain(richtext.render("hi")).endswith("\n")
assert not plain(richtext.render("hi\n\n\n")).endswith("\n\n"), "no trailing blank run"
print("blank-line collapsing OK")

# ---- CRLF ----
assert plain(richtext.render("a\r\nb")) == "a\nb\n"
print("CRLF OK")

# ---- it actually renders into a widget, with Markdown outranking body tags ----
root = tk.Tk()
text = tk.Text(root)
text.tag_configure("agent", lmargin1=12, foreground="#111827")
richtext.configure_tags(text, "Segoe UI", "Consolas", COLORS)
richtext.insert(text, "### 標題\n\n一段 **粗體** 文字\n\n```\ncode\n```", ("agent",))
root.update()
body = text.get("1.0", "end")
assert "### " not in body and "**" not in body, repr(body)
assert "標題" in body and "粗體" in body and "code" in body

idx = text.search("粗體", "1.0")
names = text.tag_names(idx)
assert "agent" in names and "md_bold" in names, names
# md_bold must win the font conflict -> it is created later, so it ranks higher
order = list(text.tag_names())
assert order.index("md_bold") > order.index("agent"), order
print("widget insert + tag priority OK")
root.destroy()

# ---- tables ----
# CJK glyphs take two monospace columns; alignment depends on getting that right
assert richtext.display_width("目錄") == 4
assert richtext.display_width("abc") == 3
assert richtext.display_width("目錄abc") == 7

narrow = ("| 模式 | 結果 |\n"
          "|---|---:|\n"
          "| `read-only` | **267** |\n"
          "| danger-full-access | OK |")
s = richtext.render(narrow)
assert "|" not in plain(s), plain(s)
assert "---" not in plain(s), "the separator row is not content"
assert s[0][1][0] == "md_table_head", s[0]
assert s[1][1] == ("md_table_rule",), s[1]
# inline markup is stripped inside cells so the widths match what is drawn
assert "`" not in plain(s) and "**" not in plain(s)
# every column is padded to the same width, so the second column starts aligned
rows = [chunk for chunk, tags in s if "md_table" in tags[0]]
starts = [r.index("267") if "267" in r else r.index("OK") for r in rows if "267" in r or "OK" in r]
assert len(set(starts)) == 1, f"columns not aligned: {starts}"
print("narrow table renders as an aligned grid OK")

# a two-column table always stays a grid, and carries a hanging-indent request
wide2 = ("| 檔案 | 用途 |\n|---|---|\n"
         "| `home_new.aspx` | " + "很長的中文說明文字" * 6 + " |")
s = richtext.render(wide2)
wrap = [t for _, tags in s for t in tags if t.startswith(richtext.WRAP_TAG_PREFIX)]
assert wrap, "a wrapped table row needs a hanging indent tag"
assert wrap[0] == richtext.WRAP_TAG_PREFIX + str(len("home_new.aspx") + richtext.COLUMN_GAP)
print("two-column table keeps the grid with a hanging indent OK")

# the header rule never grows to the width of a prose column
rule = next(chunk for chunk, tags in s if tags == ("md_table_rule",))
assert all(len(seg) <= richtext.MAX_RULE_SEGMENT for seg in rule.strip().split("  ")), rule
print("header rule is capped OK")

# 3+ columns that cannot fit fall back to one block per row
cell = "非常長的中文欄位內容需要很多空間並且更長一些"        # 22 CJK -> 44 columns
wide4 = "| A | B | C |\n|---|---|---|\n| " + " | ".join([cell] * 3) + " |"
assert richtext.display_width(cell) * 3 > richtext.MAX_TABLE_WIDTH, "fixture must exceed the cap"
s = richtext.render(wide4)
kinds = [tags[0] for _, tags in s]
assert "md_table_key" in kinds and "md_table_val" in kinds, kinds
assert "md_table" not in kinds, "a table this wide must not be laid out as a grid"
assert "B: " in plain(s), "block rows label each cell with its header"
print("wide multi-column table falls back to blocks OK")

# a table with only a header and separator degrades gracefully
s = richtext.render("| a | b |\n|---|---|")
assert plain(s).count("\n") >= 1 and "|" not in plain(s), plain(s)

# text right after a table is not swallowed
s = richtext.render("| a |\n|---|\n| 1 |\n\n之後的段落")
assert tags_for(s, "之後的段落") == ()
print("table block ends cleanly OK")

print("\nALL RICHTEXT TESTS PASSED")
