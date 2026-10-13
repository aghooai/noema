"""Trusted validation of a job's `out/` directory (ADR-002 §6, spec-09 §6.6). Stdlib only.

The manifest and every output were written inside the jail, so they are untrusted. `validate` checks the manifest
(≤ 1 MiB, strict JSON, the closed schema and enums), then each listed output (path regex, `O_NOFOLLOW` regular file
with one link, size, sha256, magic bytes, strict UTF-8 without NUL for text, the per-kind size cap, count and
aggregate budget),
deletes every file the manifest does not list, and returns the `ExtractResult`. Any violation raises
`ManifestInvalid` (the caller maps it to `failed{extraction_failed}` plus a `sandbox.manifest_invalid` event).

Result conventions (shared by every backend): `facts` of `ok`/`rejected` results are the manifest facts plus
`"warnings"` (when non-empty); `failed`/`timeout`/`violation` results carry `{"error": <reason>}` with reason in
`manifest_invalid`, `exit`, `killed`, `bad_request`, `unavailable`, `busy` (no sandboxd slot in time),
`spawn_failed`, `timeout`, `violation`.
"""

from __future__ import annotations

import codecs
import hashlib
import json
import os
import re
import shutil
import stat
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Final, Literal

from noema.core.protocols.sandbox import ExtractResult, SandboxKind, SandboxOutput
from noema.core.values import JsonValue
from noema.media.sandbox.limits import KINDS, OUTPUT_RULES, check_facts

MANIFEST_NAME: Final = "manifest.json"
MAX_MANIFEST_BYTES: Final = 1024 * 1024
PATH_RE: Final = re.compile(r"out/[a-z0-9_-]{1,64}\.(png|jpg|pdf|md|txt)")
SHA256_RE: Final = re.compile(r"[0-9a-f]{64}")
REJECTION_CODES: Final = frozenset(
    {
        "pixel_limit",
        "decompression_bomb",
        "corrupt",
        "encrypted_pdf",
        "too_many_pages",
        "empty_content",
        "type_mismatch",
    }
)
WARNINGS: Final = frozenset({"sanitize_incomplete", "frames_dropped"})
MAGIC: Final[Mapping[str, tuple[bytes, ...]]] = {
    "image/png": (b"\x89PNG\r\n\x1a\n",),
    "image/jpeg": (b"\xff\xd8\xff",),
    "application/pdf": (b"%PDF-",),
}
TEXT_TYPES: Final = frozenset({"text/markdown", "text/plain"})
RESERVED_TAG_RE: Final = re.compile(r"<\s*/?\s*(untrusted_input|app_question|decision_context|question|answer)\b", re.I)
_TOP_KEYS: Final = frozenset({"v", "kind", "extractor", "outputs", "facts", "rejection", "warnings"})
_OUTPUT_REQUIRED: Final = frozenset({"path", "kind", "media_type", "bytes", "sha256"})
_OUTPUT_OPTIONAL: Final = frozenset({"page", "width", "height"})
_CHUNK: Final = 1024 * 1024


class ManifestInvalid(Exception):
    """The job's manifest or outputs failed trusted validation; `reason` is content-free."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def _reject_constant(name: str) -> None:
    raise ManifestInvalid(f"non-finite number {name}")


def _no_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in pairs:
        if key in out:
            raise ManifestInvalid(f"duplicate key {key!r}")
        out[key] = value
    return out


def _open_regular(path: Path) -> tuple[int, os.stat_result]:
    """An fd of `path` opened without following links; ManifestInvalid unless it is a regular file with 1 link."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    except OSError:
        raise ManifestInvalid(f"{path.name} cannot be opened without following links") from None
    st = os.fstat(fd)
    if not stat.S_ISREG(st.st_mode) or st.st_nlink != 1:
        os.close(fd)
        raise ManifestInvalid(f"{path.name} is not a regular single-link file")
    return fd, st


def read_manifest(out_dir: Path) -> dict[str, Any]:
    """The decoded manifest (strict JSON object ≤ 1 MiB: no NaN/Infinity, no duplicate keys)."""
    fd, st = _open_regular(out_dir / MANIFEST_NAME)
    try:
        if st.st_size > MAX_MANIFEST_BYTES:
            raise ManifestInvalid("manifest larger than 1 MiB")
        with os.fdopen(os.dup(fd), "rb") as fh:
            raw = fh.read(MAX_MANIFEST_BYTES + 1)
    finally:
        os.close(fd)
    if len(raw) > MAX_MANIFEST_BYTES:
        raise ManifestInvalid("manifest larger than 1 MiB")
    try:
        doc = json.loads(raw.decode("utf-8"), object_pairs_hook=_no_duplicates, parse_constant=_reject_constant)
    except (UnicodeDecodeError, ValueError, RecursionError):  # RecursionError: nesting deeper than the stack
        raise ManifestInvalid("manifest is not strict UTF-8 JSON") from None
    if not isinstance(doc, dict):
        raise ManifestInvalid("manifest is not an object")
    return doc


def _int_or_none(entry: Mapping[str, Any], key: str) -> int | None:
    value = entry.get(key)
    if value is None:
        return None
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise ManifestInvalid(f"output {key} must be a positive integer")
    return value


def _check_content(path: Path, media_type: str, size: int, digest: str, cap: int) -> None:
    """Size, sha256, magic bytes and (text) strict UTF-8 without NUL, all read from one O_NOFOLLOW fd."""
    fd, st = _open_regular(path)
    try:
        if st.st_size != size or size > cap:
            raise ManifestInvalid(f"{path.name}: size {st.st_size} differs from the manifest or exceeds the cap")
        h = hashlib.sha256()
        head = b""
        decoder = codecs.getincrementaldecoder("utf-8")("strict") if media_type in TEXT_TYPES else None
        with os.fdopen(os.dup(fd), "rb") as fh:
            while chunk := fh.read(_CHUNK):
                h.update(chunk)
                if len(head) < 16:
                    head += chunk[:16]
                if decoder is not None:
                    if b"\x00" in chunk:
                        raise ManifestInvalid(f"{path.name}: NUL byte in text output")
                    decoder.decode(chunk)
            if decoder is not None:
                decoder.decode(b"", final=True)
    except UnicodeDecodeError:
        raise ManifestInvalid(f"{path.name}: text output is not strict UTF-8") from None
    finally:
        os.close(fd)
    if h.hexdigest() != digest:
        raise ManifestInvalid(f"{path.name}: sha256 differs from the manifest")
    magics = MAGIC.get(media_type)
    if magics is not None and not any(head.startswith(m) for m in magics):
        raise ManifestInvalid(f"{path.name}: magic bytes do not match {media_type}")


def _outputs(
    kind: SandboxKind, doc: Mapping[str, Any], out_dir: Path, max_outputs: int, cap: int, budget: int | None
) -> list[SandboxOutput]:
    entries = doc.get("outputs")
    if not isinstance(entries, list):
        raise ManifestInvalid("outputs must be a list")
    if len(entries) > max_outputs:
        raise ManifestInvalid(f"{len(entries)} outputs exceed max_outputs {max_outputs}")
    rules = {r.kind: r for r in OUTPUT_RULES[kind]}
    seen_paths: set[str] = set()
    seen: set[tuple[str, int | None]] = set()
    result: list[SandboxOutput] = []
    total = 0
    for entry in entries:
        if not isinstance(entry, dict) or not _OUTPUT_REQUIRED <= set(entry) <= _OUTPUT_REQUIRED | _OUTPUT_OPTIONAL:
            raise ManifestInvalid("output entry keys do not match the schema")
        path, out_kind, media_type = entry["path"], entry["kind"], entry["media_type"]
        size, digest = entry["bytes"], entry["sha256"]
        if not isinstance(path, str) or not PATH_RE.fullmatch(path) or path in seen_paths:
            raise ManifestInvalid("output path is invalid or repeated")
        rule = rules.get(out_kind) if isinstance(out_kind, str) else None
        ext = path.rsplit(".", 1)[1]
        if rule is None or (media_type, ext) not in rule.types:
            raise ManifestInvalid(f"output kind/media_type/extension not allowed for {kind}")
        if not isinstance(size, int) or isinstance(size, bool) or size < 0:
            raise ManifestInvalid("output bytes must be a non-negative integer")
        total += size  # each size is checked against the file below, before the next one is hashed
        if budget is not None and total > budget:
            raise ManifestInvalid(f"outputs exceed the {kind} output budget of {budget} bytes")
        if not isinstance(digest, str) or not SHA256_RE.fullmatch(digest):
            raise ManifestInvalid("output sha256 must be 64 lower-case hex digits")
        page, width, height = (_int_or_none(entry, k) for k in ("page", "width", "height"))
        if (page is not None) != rule.paged or (out_kind, page) in seen:
            raise ManifestInvalid("output page is missing, unexpected or repeated")
        seen_paths.add(path)
        seen.add((out_kind, page))
        file_path = out_dir / path.removeprefix("out/")
        _check_content(file_path, media_type, size, digest, cap)
        result.append(SandboxOutput(file_path, out_kind, media_type, size, digest, page, width, height))
    return result


def delete_unlisted(out_dir: Path, keep: set[str]) -> None:
    """Remove every entry of `out_dir` whose name is not in `keep`, never following links."""
    with os.scandir(out_dir) as it:
        for entry in it:
            if entry.name in keep:
                continue
            if entry.is_dir(follow_symlinks=False):
                shutil.rmtree(entry.path, ignore_errors=True)
            else:
                Path(entry.path).unlink(missing_ok=True)


def validate(
    kind: SandboxKind,
    out_dir: Path,
    *,
    max_outputs: int,
    output_cap_bytes: int,
    wall_ms: int,
    max_total_bytes: int | None = None,
) -> ExtractResult:
    """Validate `out_dir` of a finished `ok` job and return its `ok` or `rejected` result (see the module
    docstring; `max_total_bytes` caps the sum of the outputs). Unlisted files are deleted, also when validation
    fails."""
    keep = {MANIFEST_NAME}
    try:
        doc = read_manifest(out_dir)
        if set(doc) != _TOP_KEYS or doc["v"] != 1 or doc["kind"] != kind or kind not in KINDS:
            raise ManifestInvalid("manifest keys, version or kind do not match")
        extractor = doc["extractor"]
        if not isinstance(extractor, str) or not 0 < len(extractor) <= 128:
            raise ManifestInvalid("extractor must be a short string")
        facts, warnings, rejection = doc["facts"], doc["warnings"], doc["rejection"]
        if not isinstance(facts, dict) or (reason := check_facts(kind, facts)) is not None:
            raise ManifestInvalid(reason or "facts must be an object")
        if not isinstance(warnings, list) or not all(w in WARNINGS for w in warnings):
            raise ManifestInvalid("warnings must be a list of known values")
        code: str | None = None
        if rejection is not None:
            if (
                not isinstance(rejection, dict)
                or set(rejection) != {"code"}
                or rejection["code"] not in REJECTION_CODES
            ):
                raise ManifestInvalid("rejection must be null or {code: <known code>}")
            code = rejection["code"]
        outputs = _outputs(kind, doc, out_dir, max_outputs, output_cap_bytes, max_total_bytes)
        if code is not None and outputs:
            raise ManifestInvalid("a rejection lists outputs")
        keep |= {o.path.name for o in outputs}
    finally:
        delete_unlisted(out_dir, keep)
    result_facts: dict[str, JsonValue] = dict(facts)
    if warnings:
        result_facts["warnings"] = sorted(set(warnings))
    if code is None:
        return ExtractResult("ok", tuple(outputs), result_facts, None, wall_ms, extractor)
    return ExtractResult("rejected", (), result_facts, code, wall_ms, extractor)


FailStatus = Literal["failed", "timeout", "violation"]


def failure(status: FailStatus, reason: str, *, wall_ms: int, extractor: str, **extra: JsonValue) -> ExtractResult:
    """A non-ok result (`failed`, `timeout` or `violation`) carrying `{"error": reason, **extra}`."""
    return ExtractResult(status, (), {"error": reason, **extra}, None, wall_ms, extractor)


def reserved_tag_count(text: str) -> int:
    """How many reserved-tag openings a text output holds (metrics only; escaping happens at prompt render)."""
    return sum(1 for _ in RESERVED_TAG_RE.finditer(text))
