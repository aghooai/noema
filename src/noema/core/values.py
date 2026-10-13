"""Shared value types (spec-02 §1): usage, cost, degradations and provider-neutral content blocks."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Literal

type JsonValue = bool | int | float | str | list[JsonValue] | dict[str, JsonValue] | None

Effort = Literal["none", "low", "medium", "high", "xhigh", "max"]
DataClass = Literal["public", "internal", "confidential", "restricted"]
ContentPolicy = Literal["full", "redacted", "metadata_only"]
# Hierarchical round-1 and round-2 calls are decision_member calls too (no extra kind; IMPL-027).
CallKind = Literal["decision_member", "verification_member", "generation", "labeling"]
UsageLineName = Literal["decision", "generation", "labeling", "verification", "waste", "cache_savings"]
Modality = Literal["text", "image", "document"]  # Phase 1 (audio/video uploads are 415)
DegradationReason = Literal[
    "model_capability",
    "size_limit",
    "media_budget",
    "policy",
    "param_unsupported",
    "active_content_removed",
    "not_ready",
    "extraction_failed",
    "rejected",
]


@dataclass(frozen=True, slots=True)
class UsageRecord:
    """Token usage of one or more calls. Categories are disjoint except that `input_tokens` includes cache reads
    and cache writes (03:L314)."""

    input_tokens: int
    output_tokens: int
    cached_input_tokens: int = 0
    cache_write_input_tokens: int = 0
    reasoning_tokens: int = 0
    tokens_estimated: bool = False

    def __add__(self, other: UsageRecord) -> UsageRecord:
        """Field-wise sum; the result is estimated if either side is."""
        return UsageRecord(
            input_tokens=self.input_tokens + other.input_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
            cached_input_tokens=self.cached_input_tokens + other.cached_input_tokens,
            cache_write_input_tokens=self.cache_write_input_tokens + other.cache_write_input_tokens,
            reasoning_tokens=self.reasoning_tokens + other.reasoning_tokens,
            tokens_estimated=self.tokens_estimated or other.tokens_estimated,
        )


@dataclass(frozen=True, slots=True)
class Cost:
    """A USD amount (Decimal math, IMPL-019); `estimated` when any price component was TBD or usage estimated."""

    usd: Decimal
    estimated: bool


@dataclass(frozen=True, slots=True)
class Degradation:
    """Mirrors the openapi `Degradation` object (`from_` is the wire field `from`)."""

    from_: str
    to: str
    reason: DegradationReason
    file_id: str | None = None
    part_index: int | None = None
    attempt_id: str | None = None
    model: str | None = None

    def to_wire(self) -> dict[str, JsonValue]:
        """The openapi `Degradation` object: `from`, `to`, `reason`, then the set optionals in field order
        (`file_id`, `part_index`, `attempt_id`, `model`); None fields are omitted, never null."""
        optional: tuple[tuple[str, str | int | None], ...] = (
            ("file_id", self.file_id),
            ("part_index", self.part_index),
            ("attempt_id", self.attempt_id),
            ("model", self.model),
        )
        out: dict[str, JsonValue] = {"from": self.from_, "to": self.to, "reason": self.reason}
        out.update({key: value for key, value in optional if value is not None})
        return out


@dataclass(frozen=True, slots=True)
class TextBlock:
    """Plain text content."""

    text: str


@dataclass(frozen=True, slots=True)
class ImageBlock:
    """A re-encoded image derivative; `width`/`height` come from its manifest (TokenEstimator)."""

    format: Literal["png", "jpeg", "webp", "gif"]
    data: bytes
    width: int
    height: int


@dataclass(frozen=True, slots=True)
class DocumentBlock:
    """A document; `name` is the generated label (`doc-N`), never a filename."""

    format: Literal["pdf", "md", "txt"]
    name: str
    data: bytes
    pages: int = 1


ContentBlock = TextBlock | ImageBlock | DocumentBlock


@dataclass(frozen=True, slots=True)
class Message:
    """One conversation message of provider-neutral content blocks (the adapters map them, spec-02 §4.2)."""

    role: Literal["user", "assistant"]
    content: tuple[ContentBlock, ...]
