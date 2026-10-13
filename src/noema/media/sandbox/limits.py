"""Per-kind sandbox limits, params and facts (ADR-002 §5). Stdlib only: imported by the media-worker, sandboxd and
the extractors.

The limits are the ADR's initial values; `clamp` caps every requested limit at the table maximum, so a caller can
lower but never raise them. `params` and `facts` are closed per kind (`additionalProperties: false`).
"""

from __future__ import annotations

import itertools
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Final, get_args

from noema.core.protocols.sandbox import SandboxKind, SandboxLimits
from noema.core.values import JsonValue

KINDS: Final[tuple[SandboxKind, ...]] = get_args(SandboxKind)
KIND_LIMITS: Final[Mapping[SandboxKind, SandboxLimits]] = {
    "detect": SandboxLimits(wall_s=15, cpu_s=10, mem_mib=3072, fsize_mib=1, nproc=32, max_outputs=0),
    "image.normalize": SandboxLimits(wall_s=30, cpu_s=20, mem_mib=2048, fsize_mib=64, nproc=16, max_outputs=3),
    "pdf.sanitize": SandboxLimits(wall_s=60, cpu_s=45, mem_mib=2048, fsize_mib=100, nproc=16, max_outputs=1),
    "pdf.render": SandboxLimits(wall_s=300, cpu_s=280, mem_mib=2048, fsize_mib=64, nproc=16, max_outputs=600),
    "pdf.markdown": SandboxLimits(wall_s=900, cpu_s=900, mem_mib=None, fsize_mib=64, nproc=64, max_outputs=1),
    "ocr": SandboxLimits(wall_s=300, cpu_s=280, mem_mib=1024, fsize_mib=16, nproc=16, max_outputs=1),
    # C1 review evidence (media_pipeline.md): a 5 MiB Markdown upload (the cap) needs ~30 s CPU and ~1.3 GiB RSS
    "markdown.sanitize": SandboxLimits(wall_s=90, cpu_s=60, mem_mib=2048, fsize_mib=16, nproc=8, max_outputs=1),
}
_MIB: Final = 1024 * 1024
# The aggregate size of one job's outputs. The spool is one 4 GiB tmpfs shared by every job, and the per-file limit
# alone would let `pdf.render` write 600 x 64 MiB; the extractor stops at the budget (the job fails) and the trusted
# validator refuses a manifest over it.
OUTPUT_BUDGET_BYTES: Final[Mapping[str, int]] = {
    "detect": 0,
    "image.normalize": 192 * _MIB,
    "pdf.sanitize": 100 * _MIB,
    "pdf.render": 512 * _MIB,
    "pdf.markdown": 64 * _MIB,
    "ocr": 16 * _MIB,
    "markdown.sanitize": 16 * _MIB,
}
IMAGE_FORMATS: Final = ("PNG", "JPEG", "WEBP", "GIF", "HEIF")
RENDER_LONG_EDGE_PX: Final = 1568
MAX_PAGES: Final = 600
ACTIVE_CONTENT: Final = (
    "additional_actions",
    "embedded_file",
    "javascript",
    "launch",
    "open_action",
    "remote_action",
    "rich_media",
    "xfa",
)


class BadParams(ValueError):
    """Params outside the closed per-kind schema (sandboxd answers `bad_request`)."""


def clamp(kind: SandboxKind, requested: SandboxLimits | None = None) -> SandboxLimits:
    """The effective limits: each requested value capped at the table maximum (None = the table value)."""
    top = KIND_LIMITS[kind]
    if requested is None:
        return top
    if top.mem_mib is None:
        mem = requested.mem_mib
    else:
        mem = top.mem_mib if requested.mem_mib is None else min(requested.mem_mib, top.mem_mib)
    return SandboxLimits(
        wall_s=min(max(requested.wall_s, 0.1), top.wall_s),
        cpu_s=min(max(requested.cpu_s, 1), top.cpu_s),
        mem_mib=mem,
        fsize_mib=min(max(requested.fsize_mib, 1), top.fsize_mib),
        nproc=min(max(requested.nproc, 1), top.nproc),
        max_outputs=min(max(requested.max_outputs, 0), top.max_outputs),
    )


def _int_in(value: object, lo: int, hi: int) -> bool:
    """`value` is an int (not a bool) in lo..hi."""
    return isinstance(value, int) and not isinstance(value, bool) and lo <= value <= hi


def _ascending_pages(value: object, hi: int) -> bool:
    """`value` is a list of distinct ascending page numbers in 1..hi."""
    if not isinstance(value, list) or not all(_int_in(p, 1, hi) for p in value):
        return False
    return all(a < b for a, b in itertools.pairwise(value))


def check_params(kind: SandboxKind, params: Mapping[str, JsonValue]) -> dict[str, JsonValue]:
    """Validate `params` against the closed per-kind schema; returns a plain copy (with the pdf.render and ocr
    defaults filled). Raises `BadParams`."""
    allowed: Mapping[SandboxKind, frozenset[str]] = {
        "image.normalize": frozenset({"format"}),
        "pdf.render": frozenset({"long_edge_px"}),
        "ocr": frozenset({"pages"}),
    }
    if unknown := set(params) - allowed.get(kind, frozenset()):
        raise BadParams(f"unknown params for {kind}: {sorted(unknown)}")
    out = dict(params)
    if kind == "image.normalize" and out.get("format") not in IMAGE_FORMATS:
        raise BadParams("image.normalize needs params.format in PNG|JPEG|WEBP|GIF|HEIF")
    if kind == "pdf.render" and not _int_in(out.setdefault("long_edge_px", RENDER_LONG_EDGE_PX), 64, 4096):
        raise BadParams("pdf.render params.long_edge_px must be an integer in 64..4096")
    if kind == "ocr":
        pages = out["pages"] = out.get("pages")
        if pages is not None and not _ascending_pages(pages, MAX_PAGES):
            raise BadParams("ocr params.pages must be null or ascending distinct page numbers 1..600")
    return out


@dataclass(frozen=True, slots=True)
class OutputRule:
    """What a job kind may output: derivative kind, allowed (media type, extension) pairs, whether `page` is set."""

    kind: str
    types: tuple[tuple[str, str], ...]
    paged: bool = False


OUTPUT_RULES: Final[Mapping[SandboxKind, tuple[OutputRule, ...]]] = {
    "detect": (),
    "image.normalize": (
        OutputRule("raster", (("image/png", "png"),)),
        OutputRule("raster_1568", (("image/png", "png"), ("image/jpeg", "jpg"))),
        OutputRule("thumbnail", (("image/png", "png"),)),
    ),
    "pdf.sanitize": (OutputRule("pdf_sanitized", (("application/pdf", "pdf"),)),),
    "pdf.render": (OutputRule("page_images", (("image/png", "png"),), paged=True),),
    "pdf.markdown": (OutputRule("markdown", (("text/markdown", "md"),)),),
    "ocr": (OutputRule("ocr_text", (("text/plain", "txt"),)),),
    "markdown.sanitize": (OutputRule("markdown", (("text/markdown", "md"),)),),
}


def _detect_ok(facts: Mapping[str, JsonValue]) -> bool:
    label, mime, score = facts["magika_label"], facts["libmagic_mime"], facts["magika_score"]
    return (
        isinstance(label, str)
        and len(label) <= 64
        and isinstance(mime, str)
        and len(mime) <= 128
        and isinstance(score, int | float)
        and not isinstance(score, bool)
        and 0 <= score <= 1
        and _int_in(facts["bytes"], 0, 1 << 40)
        and isinstance(facts["utf8_strict"], bool)
        and isinstance(facts["has_nul"], bool)
    )


def _sanitize_ok(facts: Mapping[str, JsonValue]) -> bool:
    active = facts["active_content"]
    if not (_int_in(facts["pages"], 1, MAX_PAGES) and facts["encrypted"] is False and isinstance(active, list)):
        return False
    names = [a for a in active if isinstance(a, str) and a in ACTIVE_CONTENT]
    return len(names) == len(active) and names == sorted(set(names))


_FACT_KEYS: Final[Mapping[SandboxKind, frozenset[str]]] = {
    "detect": frozenset({"magika_label", "magika_score", "libmagic_mime", "bytes", "utf8_strict", "has_nul"}),
    "image.normalize": frozenset({"width", "height", "frames"}),
    "pdf.sanitize": frozenset({"pages", "encrypted", "active_content"}),
    "pdf.render": frozenset({"pages", "text_layer_pages"}),
    "pdf.markdown": frozenset({"pages"}),
}


def check_facts(kind: SandboxKind, facts: Mapping[str, JsonValue]) -> str | None:
    """None when `facts` match the closed per-kind schema (ADR-002 §5), else the reason. An empty mapping is valid
    for every kind (a rejection may come before any fact is known)."""
    if not facts:
        return None
    if set(facts) != _FACT_KEYS.get(kind, frozenset()):
        return f"facts keys {sorted(facts)} do not match the {kind} schema"
    if kind == "detect":
        ok = _detect_ok(facts)
    elif kind == "image.normalize":
        ok = all(_int_in(facts[k], 1, 1 << 31) for k in ("width", "height", "frames"))
    elif kind == "pdf.sanitize":
        ok = _sanitize_ok(facts)
    elif kind == "pdf.render":
        pages = facts["pages"]
        ok = isinstance(pages, int) and _int_in(pages, 1, MAX_PAGES)
        ok = ok and _ascending_pages(facts["text_layer_pages"], pages if isinstance(pages, int) else 0)
    else:  # pdf.markdown
        ok = _int_in(facts["pages"], 1, MAX_PAGES)
    return None if ok else f"invalid {kind} facts"
