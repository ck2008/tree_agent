"""Dependency-tolerant checks for the PDF attachment helpers."""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tree_agent import pdf_support


tmp = tempfile.mkdtemp()
missing = pdf_support.inspect_pdf(os.path.join(tmp, "missing.pdf"), tmp)
assert not missing.ok
assert missing.page_count == 0
assert "找不到" in missing.errors[0]
assert missing.as_dict()["rendered_images"] == []
print("missing files return structured errors OK")

not_pdf = os.path.join(tmp, "note.txt")
with open(not_pdf, "w", encoding="utf-8") as stream:
    stream.write("not a PDF")
wrong_type = pdf_support.inspect_pdf(not_pdf, tmp)
assert not wrong_type.ok
assert "不是 PDF" in wrong_type.errors[0]
print("non-PDF files are rejected safely OK")

if pdf_support.pymupdf_available():
    fitz = pdf_support._load_pymupdf()
    document = fitz.open()
    page = document.new_page()
    page.insert_text((72, 72), "Tree Agent PDF")
    source = os.path.join(tmp, "sample.pdf")
    document.save(source)
    document.close()

    rendered = os.path.join(tmp, "rendered")
    result = pdf_support.inspect_pdf(source, rendered, page_limit=10)
    assert result.ok, result.errors
    assert result.page_count == 1
    assert "Tree Agent PDF" in result.text
    assert len(result.rendered_images) == 1
    assert os.path.isfile(result.rendered_images[0])
    thumb = pdf_support.render_pdf_thumbnail(source, os.path.join(tmp, "thumb"))
    assert thumb.ok and len(thumb.rendered_images) == 1, thumb.errors
    print("PyMuPDF text extraction and page rendering OK")
else:
    print("PyMuPDF not installed; graceful dependency fallback OK")

print("\nALL PDF SUPPORT TESTS PASSED")
