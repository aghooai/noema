"""MED-03 (images), MED-04 (PDF sanitization), Markdown, render and limits, run through the InprocRlimitRunner
(real child processes; the media image runs the same tests under the image's libraries)."""

from __future__ import annotations

import io
import shutil
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path

import anyio
import pytest

from noema.core.protocols.sandbox import ExtractResult
from noema.media.sandbox.limits import KIND_LIMITS

from . import media_fixtures as fx
from .conftest import Extract

pytestmark = pytest.mark.unit


def output(result: ExtractResult, kind: str) -> bytes:
    found = [o for o in result.outputs if o.kind == kind]
    assert len(found) == 1, (kind, result)
    return found[0].path.read_bytes()


def no_metadata(data: bytes) -> None:
    from PIL import Image

    with Image.open(io.BytesIO(data)) as img:
        assert not img.getexif(), "EXIF survived"
        assert "icc_profile" not in img.info and "xmp" not in img.info and "exif" not in img.info
    assert b"CanaryCam" not in data


async def test_exif_rotated_jpeg_is_upright_and_metadata_free(extract: Extract) -> None:
    from PIL import Image

    result = await extract("image.normalize", fx.jpeg_with_exif(80, 40), {"format": "JPEG"})
    assert result.status == "ok" and result.facts["width"] == 40 and result.facts["height"] == 80
    for kind in ("raster", "raster_1568", "thumbnail"):
        data = output(result, kind)
        no_metadata(data)
        with Image.open(io.BytesIO(data)) as img:
            assert img.height > img.width  # orientation 6 applied
            pixel = img.convert("RGB").getpixel((img.width // 2, 2))
            assert isinstance(pixel, tuple) and pixel[2] < 60  # the yellow band is now on top


@pytest.mark.parametrize("fmt", ["GIF", "WEBP"])
async def test_animated_images_keep_frame_zero(extract: Extract, fmt: str) -> None:
    from PIL import Image

    result = await extract("image.normalize", fx.animated(fmt, 3), {"format": fmt})
    assert result.status == "ok" and result.facts["frames"] == 3 and result.facts.get("warnings") == ["frames_dropped"]
    with Image.open(io.BytesIO(output(result, "raster"))) as img:
        pixel = img.convert("RGB").getpixel((5, 5))
        assert getattr(img, "n_frames", 1) == 1 and isinstance(pixel, tuple) and pixel[0] == 0


async def test_heic_becomes_png(extract: Extract) -> None:
    result = await extract("image.normalize", fx.heic(fx.png(96, 64)), {"format": "HEIF"})
    assert result.status == "ok" and (result.facts["width"], result.facts["height"]) == (96, 64)
    assert output(result, "raster").startswith(b"\x89PNG")


IMAGE_REJECTIONS: dict[str, tuple[Callable[[], bytes], str, str]] = {  # name -> (builder, format, code)
    "pixel bomb 9500x9500 header": (lambda: fx.png_header_only(9_500, 9_500), "PNG", "pixel_limit"),
    "pixel bomb 50k": (lambda: fx.png_header_only(50_000, 50_000), "PNG", "pixel_limit"),
    "zTXt bomb": (lambda: fx.png_ztxt_bomb(64), "PNG", "decompression_bomb"),
    "truncated jpeg": (lambda: fx.truncated(fx.jpeg(256, 256), 0.3), "JPEG", "corrupt"),
    "truncated heic": (lambda: fx.truncated(fx.heic(), 0.4), "HEIF", "corrupt"),
    "not an image": (lambda: b"\x89PNG\r\n\x1a\n" + b"\x00" * 64, "PNG", "corrupt"),
    "gif canvas bomb": (fx.gif_canvas_bomb, "GIF", "pixel_limit"),
}


@pytest.mark.parametrize("name", sorted(IMAGE_REJECTIONS))
async def test_image_rejections(extract: Extract, name: str) -> None:
    build, fmt, code = IMAGE_REJECTIONS[name]
    result = await extract("image.normalize", build(), {"format": fmt})
    assert result.status == "rejected" and result.rejection_code == code, name
    assert result.outputs == ()


async def test_webp_canvas_bomb_is_a_typed_rejection(extract: Extract) -> None:
    result = await extract("image.normalize", fx.webp_canvas_bomb(), {"format": "WEBP"})
    assert result.status == "rejected" and result.rejection_code in ("pixel_limit", "corrupt")


async def test_long_edge_is_capped_and_never_upscaled(extract: Extract) -> None:
    wide = await extract("image.normalize", fx.png(8_200, 10), {"format": "PNG"})
    assert wide.status == "ok" and wide.facts["width"] == 8_200
    raster = next(o for o in wide.outputs if o.kind == "raster")
    assert (raster.width, raster.height) == (8_000, 10)
    small = await extract("image.normalize", fx.png(20, 10), {"format": "PNG"})
    assert {(o.kind, o.width) for o in small.outputs} == {("raster", 20), ("raster_1568", 20), ("thumbnail", 20)}


async def test_noisy_png_model_rendition_switches_to_jpeg(extract: Extract) -> None:
    result = await extract("image.normalize", fx.noisy_png(1568), {"format": "PNG"})
    model = next(o for o in result.outputs if o.kind == "raster_1568")
    assert model.media_type == "image/jpeg" and model.path.name == "raster_1568.jpg" and model.bytes <= 3_750_000
    assert output(result, "raster").startswith(b"\x89PNG")


@pytest.mark.parametrize("name", sorted(fx.malicious_pdfs()))
async def test_pdf_active_content_is_removed(extract: Extract, name: str, tmp_path: Path) -> None:
    import pikepdf

    from noema.media.extractors.pdf_sanitize import Scanner

    data, expected = fx.malicious_pdfs()[name]
    result = await extract("pdf.sanitize", data)
    assert result.status == "ok", result
    assert set(result.facts["active_content"]) == expected  # type: ignore[arg-type]
    assert result.facts["pages"] == 1 and result.facts["encrypted"] is False
    clean = output(result, "pdf_sanitized")
    with pikepdf.open(io.BytesIO(clean)) as pdf:
        assert Scanner(pdf, fix=False).walk() == set()  # the post-scan holds on the stored output
    if shutil.which("qpdf"):  # the media image has the qpdf CLI (ADR-002 §8)
        target = tmp_path / f"{name}.pdf"
        target.write_bytes(clean)
        check = await anyio.run_process(["qpdf", "--check", str(target)], check=False)
        assert check.returncode == 0, check.stdout


PDF_REJECTIONS: dict[str, tuple[Callable[[], bytes], str]] = {  # name -> (builder, code)
    "encrypted, empty user password": (fx.encrypted_pdf, "encrypted_pdf"),
    "601 pages": (lambda: fx.many_pages_pdf(601), "too_many_pages"),
    "0 pages": (lambda: fx.many_pages_pdf(0), "empty_content"),
    "garbage": (lambda: b"%PDF-1.7\n" + b"\x00garbage" * 50, "corrupt"),
    # Wave E (QA security fuzz findings 19-20): damaged PDFs pikepdf opens were failed{extraction_failed}, not corrupt
    "truncated (None in pdf.objects)": (lambda: fx.truncated(fx.text_pdf()), "corrupt"),
    "undecodable name key": (lambda: regression("sanitize-undecodable-key.pdf"), "corrupt"),
    "sanitized output without /Root": (lambda: regression("sanitize-reopen-no-root.pdf"), "corrupt"),
    "pikepdf SystemError on open": (lambda: regression("sanitize-pikepdf-systemerror.pdf"), "corrupt"),
}


def regression(name: str) -> bytes:
    """A fuzz input kept as a regression (tests/unit/media_pipeline/regressions, from tests/security/fuzz)."""
    return (Path(__file__).parent / "regressions" / name).read_bytes()


@pytest.mark.parametrize("name", sorted(PDF_REJECTIONS))
async def test_pdf_rejections(extract: Extract, name: str) -> None:
    build, code = PDF_REJECTIONS[name]
    result = await extract("pdf.sanitize", build())
    assert result.status == "rejected" and result.rejection_code == code, name


async def test_600_pages_are_accepted(extract: Extract) -> None:
    result = await extract("pdf.sanitize", fx.many_pages_pdf(600))
    assert result.status == "ok" and result.facts["pages"] == 600


async def test_render_reports_the_text_layer(extract: Extract) -> None:
    import pikepdf

    doc = pikepdf.open(io.BytesIO(fx.text_pdf("Page one text", pages=1)))
    doc.add_blank_page(page_size=(200, 200))  # page 2 has no text layer (scanned)
    buf = io.BytesIO()
    doc.save(buf)
    result = await extract("pdf.render", buf.getvalue(), {"long_edge_px": 1568})
    assert result.status == "ok" and result.facts == {"pages": 2, "text_layer_pages": [1]}
    pages = sorted(result.outputs, key=lambda o: o.page or 0)
    assert [(o.page, o.path.name) for o in pages] == [(1, "page-0001.png"), (2, "page-0002.png")]
    assert all(max(o.width or 0, o.height or 0) == 1568 for o in pages)


async def test_a_page_pdfium_cannot_load_is_a_corrupt_rejection(extract: Extract) -> None:
    """Wave E (QA security fuzz finding 19): a per-page PdfiumError ended the job failed, not rejected{corrupt}."""
    result = await extract("pdf.render", regression("render-page-load.pdf"), {"long_edge_px": 1568})
    assert result.status == "rejected" and result.rejection_code == "corrupt"


async def test_markdown_sanitize(extract: Extract) -> None:
    result = await extract("markdown.sanitize", b"# Hi\n\n<script>x</script> [a](javascript:x)\n")
    text = output(result, "markdown").decode()
    assert "\\<script\\>" in text and "](<javascript" not in text
    blank = await extract("markdown.sanitize", b" \n\t\n")
    assert blank.status == "rejected" and blank.rejection_code == "empty_content"


async def test_limits_kill_slow_and_hungry_jobs(extract: Extract) -> None:
    slow = await extract("pdf.render", fx.text_pdf(), None, replace(KIND_LIMITS["pdf.render"], wall_s=0.05))
    assert slow.status == "timeout" and slow.facts == {"error": "timeout"}
    hungry = await extract("detect", fx.png(), None, replace(KIND_LIMITS["detect"], mem_mib=48))
    assert hungry.status == "failed" and hungry.facts == {"error": "exit"}


async def test_unknown_params_are_a_failed_bad_request(extract: Extract) -> None:
    result = await extract("image.normalize", fx.png(), {"format": "PNG", "exec": "x"})
    assert result.status == "failed" and result.facts == {"error": "bad_request"}
