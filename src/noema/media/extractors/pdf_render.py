"""`pdf.render` (ADR-002 §5): pypdfium2 renders every page of `pdf_sanitized` at `params.long_edge_px` (1568) on
its long edge, without forms or annotations' appearance scripts (pdfium built without V8/XFA), and reports the
pages whose text layer has at least one non-space character. Outputs `page-NNNN.png`; facts `pages`,
`text_layer_pages[]`. A document or a page pdfium cannot load is a `corrupt` rejection."""

from __future__ import annotations

import io
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Final

from noema.core.values import JsonValue
from noema.media.extractors.common import Outputs, Rejection, version_of

EXTRACTOR: Final = version_of("pypdfium2")
MAX_PAGES: Final = 600


def open_document(input_path: Path) -> Any:
    """The pdfium document; a load failure is a `corrupt` rejection."""
    import pypdfium2  # type: ignore[import-untyped]

    try:
        return pypdfium2.PdfDocument(input_path)
    except pypdfium2.PdfiumError:
        raise Rejection("corrupt") from None


def has_text_layer(page: Any) -> bool:
    """True when the page's pdfium text page holds a non-space character."""
    textpage = page.get_textpage()
    try:
        return any(not ch.isspace() for ch in textpage.get_text_range())
    finally:
        textpage.close()


def render_png(page: Any, long_edge_px: int) -> tuple[bytes, int, int]:
    """The page rendered so its long edge is `long_edge_px`, as PNG bytes, width and height."""
    width, height = page.get_size()
    scale = long_edge_px / max(width, height, 1.0)
    bitmap = page.render(scale=scale, may_draw_forms=False)
    try:
        image = bitmap.to_pil().convert("RGB")
    finally:
        bitmap.close()
    buf = io.BytesIO()
    image.save(buf, format="PNG", compress_level=6)
    return buf.getvalue(), image.width, image.height


def run(input_path: Path, out: Outputs, params: Mapping[str, JsonValue]) -> None:
    """Render every page (see the module docstring)."""
    import pypdfium2

    long_edge = int(params.get("long_edge_px") or 1568)  # type: ignore[arg-type]
    doc = open_document(input_path)
    try:
        pages = len(doc)
        if pages == 0:
            raise Rejection("empty_content")
        if pages > MAX_PAGES:
            raise Rejection("too_many_pages")
        text_pages: list[JsonValue] = []
        for index in range(pages):
            try:
                page = doc[index]
            except pypdfium2.PdfiumError:  # a page pdfium cannot load (Wave E fuzz finding): corrupt, not failed
                raise Rejection("corrupt") from None
            try:
                if has_text_layer(page):
                    text_pages.append(index + 1)
                data, width, height = render_png(page, long_edge)
            except pypdfium2.PdfiumError:
                raise Rejection("corrupt") from None
            finally:
                page.close()
            out.add(
                f"page-{index + 1:04d}.png",
                data,
                kind="page_images",
                media_type="image/png",
                page=index + 1,
                width=width,
                height=height,
            )
    finally:
        doc.close()
    out.facts.update({"pages": pages, "text_layer_pages": text_pages})
