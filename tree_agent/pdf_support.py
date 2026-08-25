"""Small, optional PDF helpers used by the attachment workflow.

PyMuPDF is deliberately optional: Tree Agent must still start on a computer
that has not installed it.  Callers receive an inspection result with a clear
error instead of an import-time failure.  Rendered pages are regular PNG files
so either supported CLI can receive them as image attachments.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import os
from pathlib import Path
import re
from typing import Any


DEFAULT_PAGE_LIMIT = 20
DEFAULT_RENDER_SCALE = 1.5
THUMBNAIL_SCALE = 0.35


@dataclass
class PdfInspection:
    """Portable result of reading and optionally rendering a PDF."""

    path: str
    page_count: int = 0
    text: str = ""
    rendered_images: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors

    def as_dict(self) -> dict[str, Any]:
        """Return JSON-friendly metadata for a message or workspace store."""
        return {
            "path": self.path,
            "page_count": self.page_count,
            "text": self.text,
            "rendered_images": list(self.rendered_images),
            "errors": list(self.errors),
            "ok": self.ok,
        }


def pymupdf_available() -> bool:
    """Whether this Python interpreter can inspect and render PDFs."""
    return _load_pymupdf() is not None


def inspect_pdf(
    path: str,
    output_dir: str | None = None,
    *,
    page_limit: int = DEFAULT_PAGE_LIMIT,
    render_scale: float = DEFAULT_RENDER_SCALE,
    extract_text: bool = True,
    render_pages: bool = True,
) -> PdfInspection:
    """Inspect ``path``, extracting text and rendering up to ``page_limit`` pages.

    ``output_dir`` is required only when ``render_pages`` is true.  PNG paths
    are returned in page order.  Bad paths, missing PyMuPDF, corrupt documents,
    and individual page failures are reported in ``errors`` and never raise to
    the GUI event loop.
    """
    result = PdfInspection(path=os.path.abspath(path))
    if not path or not os.path.isfile(path):
        result.errors.append("找不到 PDF 檔案。")
        return result
    if not path.lower().endswith(".pdf"):
        result.errors.append("附件不是 PDF 檔案。")
        return result
    if page_limit < 1:
        result.errors.append("PDF 頁數上限必須至少為 1。")
        return result
    if render_scale <= 0:
        result.errors.append("PDF 渲染比例必須大於 0。")
        return result
    if render_pages and not output_dir:
        result.errors.append("需要指定 PDF 頁面輸出資料夾。")
        return result

    fitz = _load_pymupdf()
    if fitz is None:
        result.errors.append("未安裝 PyMuPDF；請執行 python -m pip install PyMuPDF。")
        return result

    try:
        document = fitz.open(path)
    except Exception as exc:
        result.errors.append(f"無法開啟 PDF：{_error_text(exc)}")
        return result

    try:
        result.page_count = len(document)
        pages_to_process = min(result.page_count, page_limit)
        if extract_text:
            text_parts: list[str] = []
            for number in range(pages_to_process):
                try:
                    text_parts.append(document.load_page(number).get_text("text"))
                except Exception as exc:
                    result.errors.append(f"無法擷取第 {number + 1} 頁文字：{_error_text(exc)}")
            result.text = "\n".join(part for part in text_parts if part).strip()

        if render_pages:
            target = Path(output_dir)  # validated above
            target.mkdir(parents=True, exist_ok=True)
            stem = _safe_stem(Path(path).stem)
            matrix = fitz.Matrix(render_scale, render_scale)
            for number in range(pages_to_process):
                try:
                    pixmap = document.load_page(number).get_pixmap(matrix=matrix, alpha=False)
                    image_path = target / f"{stem}-page-{number + 1}.png"
                    pixmap.save(str(image_path))
                    result.rendered_images.append(str(image_path))
                except Exception as exc:
                    result.errors.append(f"無法渲染第 {number + 1} 頁：{_error_text(exc)}")
    finally:
        document.close()
    return result


def render_pdf_thumbnail(path: str, output_dir: str, *, scale: float = THUMBNAIL_SCALE) -> PdfInspection:
    """Render the first PDF page only, suitable for an attachment thumbnail."""
    return inspect_pdf(
        path,
        output_dir,
        page_limit=1,
        render_scale=scale,
        extract_text=False,
        render_pages=True,
    )


def _load_pymupdf() -> Any | None:
    """Support both the current ``pymupdf`` name and legacy ``fitz`` package."""
    try:
        import pymupdf as fitz  # PyMuPDF 1.24+
        return fitz
    except ImportError:
        try:
            import fitz
            return fitz
        except ImportError:
            return None


def _safe_stem(value: str) -> str:
    """Use a predictable file-name stem without path or Windows-invalid chars."""
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]+', "-", value).strip(" .-")
    return cleaned or "document"


def _error_text(exc: Exception) -> str:
    text = str(exc).strip()
    return text if text else exc.__class__.__name__
