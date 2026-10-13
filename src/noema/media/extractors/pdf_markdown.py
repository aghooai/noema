"""`pdf.markdown` (ADR-002 §5): Docling over `pdf_sanitized` (`do_ocr=False`, the baked models, offline), one
`export_to_markdown(page_no=p, image_placeholder="")` per page through the spec-09 §6.5 renderer, joined with `\\f`
so `markdown.md` holds exactly `pages` segments. Facts: `pages`."""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path
from typing import Final

from noema.core.values import JsonValue
from noema.media.extractors.common import Outputs, Rejection, version_of
from noema.media.extractors.md_render import sanitize

EXTRACTOR: Final = f"{version_of('docling')}+{version_of('docling-core')}+md_render@1"
MAX_PAGES: Final = 600
DEFAULT_ARTIFACTS: Final = "/opt/docling-models"


def page_count(input_path: Path) -> int:
    """The page count of the (sanitized) PDF, from pdfium."""
    from noema.media.extractors.pdf_render import open_document

    doc = open_document(input_path)
    try:
        return len(doc)
    finally:
        doc.close()


def run(input_path: Path, out: Outputs, params: Mapping[str, JsonValue]) -> None:
    """Convert one sanitized PDF to per-page Markdown."""
    pages = page_count(input_path)  # before importing Docling (torch): a non-PDF fails fast
    if pages == 0:
        raise Rejection("empty_content")
    if pages > MAX_PAGES:
        raise Rejection("too_many_pages")
    from docling.datamodel.base_models import InputFormat
    from docling.datamodel.pipeline_options import PdfPipelineOptions
    from docling.document_converter import DocumentConverter, PdfFormatOption

    artifacts = os.environ.get("DOCLING_ARTIFACTS_PATH", DEFAULT_ARTIFACTS)
    options = PdfPipelineOptions(do_ocr=False, artifacts_path=artifacts if Path(artifacts).is_dir() else None)
    converter = DocumentConverter(format_options={InputFormat.PDF: PdfFormatOption(pipeline_options=options)})
    document = converter.convert(input_path).document
    segments = []
    for page_no in range(1, pages + 1):
        raw = document.export_to_markdown(page_no=page_no, image_placeholder="")
        segments.append(sanitize(raw).replace("\f", " ").rstrip("\n"))
    out.facts["pages"] = pages
    out.add("markdown.md", "\f".join(segments).encode("utf-8"), kind="markdown", media_type="text/markdown")
