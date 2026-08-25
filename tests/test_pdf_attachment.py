"""PDF attachments are converted into shared context for both CLI runners."""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import tempfile
import tkinter as tk

from tree_agent import pdf_support
from tree_agent import app as app_mod
from tree_agent.app import TreeAgentApp, _file_citations_to_markdown

tmp = tempfile.mkdtemp()
source = os.path.join(tmp, "brief.pdf")
with open(source, "wb") as stream:
    stream.write(b"%PDF-placeholder")
root = tk.Tk()
app = TreeAgentApp(root, home=tempfile.mkdtemp(), single_instance=False)
real_inspect = app_mod.pdf_support.inspect_pdf
requested_limits = []
def fake_inspect(path, output_dir, **kwargs):
    requested_limits.append(kwargs["page_limit"])
    rendered = os.path.join(output_dir, "brief-page-1.png")
    return pdf_support.PdfInspection(
        path=source, page_count=12, text="PDF body", rendered_images=[rendered]
    )
app_mod.pdf_support.inspect_pdf = fake_inspect
try:
    prepared = app._prepare_attachments([source])
finally:
    app_mod.pdf_support.inspect_pdf = real_inspect

assert not prepared["errors"], prepared
rendered = prepared["codex_images"][0]
assert rendered.endswith("brief-page-1.png")
assert "PDF body" in prepared["context"]
assert "前 12 頁" in prepared["context"]
assert requested_limits == [20], requested_limits
assert os.path.dirname(source) in prepared["claude_dirs"]
assert os.path.dirname(rendered) in prepared["claude_dirs"]
link = _file_citations_to_markdown(
    ':codex-file-citation{path="E:\\output\\report.pdf" purpose="output"}'
)
assert link == "[report.pdf](file:///E:/output/report.pdf)", link
app.on_close()
print("PDF attachment supplies text, page images, Claude access, and output links OK")
