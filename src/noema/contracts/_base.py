"""Base class of the generated server models (spec-08 §10.4)."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class StrictBase(BaseModel):
    """Strict, closed models: unknown fields are rejected and no type coercion happens. `regex_engine="python-re"`
    is required for the `Name` look-ahead pattern, which the Rust regex engine rejects."""

    model_config = ConfigDict(extra="forbid", strict=True, regex_engine="python-re", protected_namespaces=())
