"""`image.normalize` (spec-09 §6.4, ADR-002 §5): decode frame 0 with Pillow (+ pillow-heif for HEIF) under the
pixel-bomb guards, apply the EXIF orientation, drop every metadata chunk (only pixels are re-encoded) and write
`raster.png` (long edge ≤ 8,000, never upscaled), `raster_1568.png` (≤ 1,568, LANCZOS; `raster_1568.jpg` at quality
85 on white when the PNG exceeds 3,750,000 bytes) and `thumbnail.png` (≤ 256). Facts: the original `width` and
`height` after orientation and the `frames` count. Rejections: spec-07 §1.2."""

from __future__ import annotations

import io
import warnings
from collections.abc import Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Final

from noema.core.values import JsonValue
from noema.media.extractors.common import Outputs, Rejection, version_of

if TYPE_CHECKING:
    from PIL.Image import Image

EXTRACTOR: Final = f"{version_of('Pillow')}+{version_of('pillow-heif')}"
MAX_IMAGE_PIXELS: Final = 89_478_485
RASTER_LONG_EDGE: Final = 8_000
MODEL_LONG_EDGE: Final = 1_568
THUMBNAIL_LONG_EDGE: Final = 256
MAX_MODEL_IMAGE_BYTES: Final = 3_750_000
JPEG_QUALITY: Final = 85
_SIXTEEN_BIT: Final = frozenset({"I", "I;16", "I;16B", "I;16L", "I;16N", "F"})


def _scaled(img: Image, long_edge: int) -> Image:
    """`img` downscaled (LANCZOS) so its long edge is ≤ `long_edge`; never upscaled."""
    from PIL import Image as PILImage

    w, h = img.size
    if max(w, h) <= long_edge:
        return img
    ratio = long_edge / max(w, h)
    size = (max(1, round(w * ratio)), max(1, round(h * ratio)))
    return img.resize(size, PILImage.Resampling.LANCZOS, reducing_gap=3.0)


def _encode_png(img: Image) -> bytes:
    buf = io.BytesIO()
    img.info = {}  # no ICC/EXIF/XMP/text chunk survives
    img.save(buf, format="PNG", optimize=False, compress_level=6)
    return buf.getvalue()


def _encode_jpeg(img: Image) -> bytes:
    from PIL import Image as PILImage

    if img.mode == "RGBA":
        flat = PILImage.new("RGB", img.size, (255, 255, 255))
        flat.paste(img, mask=img.getchannel("A"))
        img = flat
    buf = io.BytesIO()
    img.info = {}
    img.save(buf, format="JPEG", quality=JPEG_QUALITY, optimize=True)
    return buf.getvalue()


def _pixels(img: Image) -> Image:
    """RGB, or RGBA when the image has alpha (no colour management)."""
    if img.mode in _SIXTEEN_BIT:
        img = img.convert("I").point(lambda v: v / 256).convert("L")
    alpha = img.mode in ("RGBA", "LA", "PA", "La", "RGBa") or img.has_transparency_data
    return img.convert("RGBA" if alpha else "RGB")


def _decode(input_path: Path, fmt: str) -> tuple[Image, int]:
    """Frame 0 of the image, upright, as RGB(A), and the frame count. Raises `Rejection` for bombs and corruption."""
    from PIL import Image as PILImage
    from PIL import ImageOps

    with PILImage.open(input_path, formats=[fmt]) as im:
        w, h = im.size
        if w * h > MAX_IMAGE_PIXELS:
            raise Rejection("pixel_limit")
        frames = max(1, int(getattr(im, "n_frames", 1) or 1))
        im.seek(0)
        im.load()
        upright = ImageOps.exif_transpose(im)
        return _pixels(upright), frames


def _classify(exc: BaseException) -> str:
    from PIL import Image as PILImage

    if isinstance(exc, PILImage.DecompressionBombError | PILImage.DecompressionBombWarning):
        return "pixel_limit"
    text = str(exc).lower()
    if "decompressed data too large" in text or "security limit" in text or "exceeds the maximum" in text:
        return "decompression_bomb"
    return "corrupt"


def run(input_path: Path, out: Outputs, params: Mapping[str, JsonValue]) -> None:
    """Normalize one image (see the module docstring)."""
    from PIL import Image as PILImage

    fmt = str(params["format"])
    PILImage.MAX_IMAGE_PIXELS = MAX_IMAGE_PIXELS
    warnings.simplefilter("error", PILImage.DecompressionBombWarning)
    if fmt == "HEIF":
        import pillow_heif

        pillow_heif.register_heif_opener()
    try:
        img, frames = _decode(input_path, fmt)
    except Rejection:
        raise
    except (MemoryError, RecursionError):
        raise
    except Exception as exc:  # every decoder failure is a typed rejection (spec-07 §1.2)
        raise Rejection(_classify(exc)) from None
    width, height = img.size
    out.facts.update({"width": width, "height": height, "frames": frames})
    if frames > 1:
        out.warn("frames_dropped")
    raster = _scaled(img, RASTER_LONG_EDGE)
    out.add(
        "raster.png",
        _encode_png(raster),
        kind="raster",
        media_type="image/png",
        width=raster.width,
        height=raster.height,
    )
    model = _scaled(raster, MODEL_LONG_EDGE)
    data = _encode_png(model)
    if len(data) <= MAX_MODEL_IMAGE_BYTES:
        out.add(
            "raster_1568.png", data, kind="raster_1568", media_type="image/png", width=model.width, height=model.height
        )
    else:
        out.add(
            "raster_1568.jpg",
            _encode_jpeg(model),
            kind="raster_1568",
            media_type="image/jpeg",
            width=model.width,
            height=model.height,
        )
    thumb = _scaled(model, THUMBNAIL_LONG_EDGE)
    out.add(
        "thumbnail.png",
        _encode_png(thumb),
        kind="thumbnail",
        media_type="image/png",
        width=thumb.width,
        height=thumb.height,
    )
