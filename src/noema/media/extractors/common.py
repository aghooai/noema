"""Shared pieces of the jailed extractors (ADR-002 §5-§6): the typed rejection, the output writer and the manifest.

Everything here runs inside the jail on untrusted input; the worker re-validates whatever it writes.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final

from noema.core.values import JsonValue
from noema.media.sandbox.limits import OUTPUT_BUDGET_BYTES

MANIFEST_NAME: Final = "manifest.json"


class Rejection(Exception):
    """The input is rejected with a FileObject rejection code (spec-07 §1.2); the job still exits 0."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class OutputBudgetExceeded(Exception):
    """The job's outputs would exceed its aggregate budget (`limits.OUTPUT_BUDGET_BYTES`): the job fails."""


@dataclass
class Outputs:
    """Collects one job's outputs in `out_dir` and writes `manifest.json` last. `budget_bytes` (default: the kind's
    `OUTPUT_BUDGET_BYTES`) caps the sum of the outputs: `add` raises `OutputBudgetExceeded` before writing past it."""

    kind: str
    out_dir: Path
    extractor: str
    outputs: list[dict[str, JsonValue]] = field(default_factory=list)
    facts: dict[str, JsonValue] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    budget_bytes: int | None = None
    written: int = 0

    def __post_init__(self) -> None:
        if self.budget_bytes is None:
            self.budget_bytes = OUTPUT_BUDGET_BYTES.get(self.kind, 0)

    def add(
        self,
        name: str,
        data: bytes,
        *,
        kind: str,
        media_type: str,
        page: int | None = None,
        width: int | None = None,
        height: int | None = None,
    ) -> None:
        """Write `out/<name>` and list it (`OutputBudgetExceeded` instead when the budget would be passed)."""
        if self.written + len(data) > (self.budget_bytes or 0):
            raise OutputBudgetExceeded(self.kind)
        (self.out_dir / name).write_bytes(data)
        self.written += len(data)
        entry: dict[str, JsonValue] = {
            "path": f"out/{name}",
            "kind": kind,
            "media_type": media_type,
            "bytes": len(data),
            "sha256": hashlib.sha256(data).hexdigest(),
        }
        entry.update({k: v for k, v in (("page", page), ("width", width), ("height", height)) if v is not None})
        self.outputs.append(entry)

    def warn(self, warning: str) -> None:
        """Record a warning (enum values only, ADR-002 §6)."""
        if warning not in self.warnings:
            self.warnings.append(warning)

    def write_manifest(self, rejection: str | None = None) -> None:
        """Write the manifest; a rejection drops every listed output and the facts."""
        doc: dict[str, JsonValue] = {
            "v": 1,
            "kind": self.kind,
            "extractor": self.extractor,
            "outputs": [] if rejection else list(self.outputs),
            "facts": {} if rejection else dict(self.facts),
            "rejection": None if rejection is None else {"code": rejection},
            "warnings": list[JsonValue](sorted(self.warnings)),
        }
        if rejection is not None:
            for entry in self.outputs:
                (self.out_dir / str(entry["path"]).removeprefix("out/")).unlink(missing_ok=True)
        (self.out_dir / MANIFEST_NAME).write_text(json.dumps(doc, separators=(",", ":")), encoding="utf-8")


def version_of(dist: str) -> str:
    """`<dist>@<installed version>` for the manifest's `extractor` field."""
    from importlib.metadata import PackageNotFoundError, version

    try:
        return f"{dist}@{version(dist)}"
    except PackageNotFoundError:
        return f"{dist}@unknown"
