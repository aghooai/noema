"""Media sandbox runner (exactly ADR-002 §7). Backends: RemoteSandboxRunner (media-worker), NsjailRunner (inside
sandboxd), BwrapRunner (host dev), InprocRlimitRunner (unit tests only)."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol

from noema.core.values import JsonValue

SandboxKind = Literal[
    "detect", "image.normalize", "pdf.sanitize", "pdf.render", "pdf.markdown", "ocr", "markdown.sanitize"
]


@dataclass(frozen=True, slots=True)
class SandboxLimits:
    """Per-job limits (ADR-002 §5)."""

    wall_s: float
    cpu_s: int
    mem_mib: int | None
    fsize_mib: int
    nproc: int
    max_outputs: int


@dataclass(frozen=True, slots=True)
class SandboxJob:
    """One extraction job over one input file."""

    job_id: str
    kind: SandboxKind
    input_path: Path
    params: Mapping[str, JsonValue]


@dataclass(frozen=True, slots=True)
class SandboxOutput:
    """One file the job produced."""

    path: Path
    kind: str
    media_type: str
    bytes: int
    sha256: str
    page: int | None = None
    width: int | None = None
    height: int | None = None


@dataclass(frozen=True, slots=True)
class ExtractResult:
    """The job outcome; `rejection_code` is set for `rejected`."""

    status: Literal["ok", "rejected", "failed", "timeout", "violation"]
    outputs: tuple[SandboxOutput, ...]
    facts: Mapping[str, JsonValue]
    rejection_code: str | None
    wall_ms: int
    extractor: str


class SandboxRunner(Protocol):
    """Runs one job inside its isolation backend."""

    async def run(self, job: SandboxJob, limits: SandboxLimits) -> ExtractResult:
        """Never raises for job failures: they are `ExtractResult.status` values."""
        ...
