"""`RequestGate`: the only request accept/reject authority (IMPL-041 point 1, IMPL-064).

Per operation of the bundled OpenAPI it checks the path, query and header parameters (coerced per their schema
type; unknown query parameters are 400; unknown headers are ignored) and the JSON body: 413 `request_too_large`,
then 415 `unsupported_media_type`, then JSON decoding (400), then the JSON Schema (400 `schema_violation` with
`param` = the first error's field path). Every validator is compiled at construction.
"""

from __future__ import annotations

import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final

import jsonschema_rs
import orjson

from noema.contracts.bundle import BUNDLE_PATH, Bundle, ParamLocation, RawOperation
from noema.core.errors import ErrorCode, NoemaError
from noema.core.values import JsonValue

DEFAULT_MAX_JSON_BODY_BYTES: Final = 52_428_800  # platform.limits.max_json_body_bytes default (IMPL-064)
JSON_MEDIA_TYPES: Final = frozenset({"application/json"})
_INT: Final = re.compile(r"-?[0-9]{1,19}")


def param_path(instance_path: Sequence[str | int]) -> str:
    """`["questions", 1, "choices"]` -> `"questions[1].choices"`; the root is `""`."""
    out = ""
    for token in instance_path:
        if isinstance(token, int):
            out += f"[{token}]"
        else:
            out += f".{token}" if out else str(token)
    return out


def media_type(content_type: str | None) -> str | None:
    """The lower-cased media type of a Content-Type header, without parameters."""
    if content_type is None:
        return None
    mt = content_type.split(";", 1)[0].strip().lower()
    return mt or None


def _violation(message: str, param: str | None) -> NoemaError:
    return NoemaError(ErrorCode.SCHEMA_VIOLATION, message, param=param or None)


def _keyword(err: jsonschema_rs.ValidationError) -> str:
    words = [p for p in err.schema_path if isinstance(p, str)]
    return words[-1] if words else "schema"


@dataclass(frozen=True, slots=True)
class _Param:
    name: str
    key: str  # lookup key: header names are lower-cased
    location: ParamLocation
    required: bool
    json_type: str | None
    has_default: bool
    default: JsonValue
    validator: jsonschema_rs.Validator


@dataclass(frozen=True, slots=True)
class _Operation:
    raw: RawOperation
    params: tuple[_Param, ...]
    query_names: frozenset[str]
    body_validator: jsonschema_rs.Validator | None
    body_media_types: frozenset[str]


class RequestGate:
    """Validates requests against one bundle. Thread-safe after construction (read-only)."""

    def __init__(self, bundle: Bundle, *, max_json_body_bytes: int = DEFAULT_MAX_JSON_BODY_BYTES) -> None:
        self.bundle: Final = bundle
        self.max_json_body_bytes: Final = max_json_body_bytes
        self._ops: dict[str, _Operation] = {}
        for raw in bundle.operations():
            params = tuple(
                _Param(
                    name=p.name,
                    key=p.name.lower() if p.location == "header" else p.name,
                    location=p.location,
                    required=p.required,
                    json_type=self._json_type(p.schema),
                    has_default="default" in p.schema,
                    default=p.schema.get("default"),
                    validator=bundle.validator(p.schema_ptr),
                )
                for p in raw.parameters
            )
            json_ptrs = [ptr for mt, ptr in raw.body_ptrs.items() if mt in JSON_MEDIA_TYPES]
            self._ops[raw.operation_id] = _Operation(
                raw=raw,
                params=params,
                query_names=frozenset(p.name for p in params if p.location == "query"),
                body_validator=bundle.validator(json_ptrs[0]) if json_ptrs else None,
                body_media_types=frozenset(raw.body_ptrs),
            )

    @classmethod
    def from_bundle(
        cls, bundle: Path = BUNDLE_PATH, *, max_json_body_bytes: int = DEFAULT_MAX_JSON_BODY_BYTES
    ) -> RequestGate:
        """A gate over a bundle file (default: the committed `build/openapi.bundled.yaml`)."""
        return cls(Bundle.load(bundle), max_json_body_bytes=max_json_body_bytes)

    def _json_type(self, schema: Mapping[str, object]) -> str | None:
        resolved = self.bundle.resolve(schema)
        t = resolved.get("type") if isinstance(resolved, Mapping) else None
        return t if isinstance(t, str) else None

    @property
    def operations(self) -> Mapping[str, RawOperation]:
        """operationId -> operation, in document order."""
        return {k: v.raw for k, v in self._ops.items()}

    def operation(self, operation_id: str) -> RawOperation:
        """Raises KeyError for an unknown operation id (a routing bug)."""
        return self._ops[operation_id].raw

    def check_params(
        self,
        operation_id: str,
        *,
        path: Mapping[str, str],
        query: Mapping[str, Sequence[str]],
        headers: Mapping[str, str],
    ) -> dict[str, JsonValue]:
        """Coerce and validate every declared parameter; returns {declared name: value} for the parameters present
        plus schema defaults. Unknown query parameters, repeated scalar values, missing required parameters and
        schema failures raise `NoemaError(schema_violation, param=<name>)`. Undeclared headers are ignored."""
        op = self._ops[operation_id]
        unknown = sorted(set(query) - op.query_names)
        if unknown:
            raise _violation(f"Unknown query parameter '{unknown[0]}'.", unknown[0])
        lowered = {k.lower(): v for k, v in headers.items()}
        out: dict[str, JsonValue] = {}
        for p in op.params:
            raw: str | None
            if p.location == "query":
                values = query.get(p.key)
                if values is not None and len(values) > 1:
                    raise _violation(f"Query parameter '{p.name}' must appear at most once.", p.name)
                raw = values[0] if values else None
            elif p.location == "path":
                raw = path.get(p.key)
            else:
                raw = lowered.get(p.key)
            if raw is None:
                if p.required:
                    raise _violation(f"Missing required parameter '{p.name}'.", p.name)
                if p.has_default:
                    out[p.name] = p.default
                continue
            value = self._coerce(p, raw)
            if not p.validator.is_valid(value):
                raise _violation(f"Invalid value for parameter '{p.name}'.", p.name)
            out[p.name] = value
        return out

    @staticmethod
    def _coerce(p: _Param, raw: str) -> JsonValue:
        if p.json_type == "integer":
            if not _INT.fullmatch(raw):
                raise _violation(f"Parameter '{p.name}' must be an integer.", p.name)
            return int(raw)
        if p.json_type == "number":
            try:
                number = float(raw)
            except ValueError:
                number = math.nan
            if not math.isfinite(number):
                raise _violation(f"Parameter '{p.name}' must be a number.", p.name)
            return number
        if p.json_type == "boolean":
            if raw not in ("true", "false"):
                raise _violation(f"Parameter '{p.name}' must be true or false.", p.name)
            return raw == "true"
        return raw

    def check_content_type(self, operation_id: str, content_type: str | None) -> str | None:
        """The declared media type matching `content_type`, or None for an operation without a request body.
        Raises `NoemaError(unsupported_media_type)` (415) when the operation declares a body of another type."""
        op = self._ops[operation_id]
        if not op.body_media_types:
            return None
        mt = media_type(content_type)
        if mt not in op.body_media_types:
            accepted = ", ".join(sorted(op.body_media_types))
            raise NoemaError(ErrorCode.UNSUPPORTED_MEDIA_TYPE, f"Content-Type must be {accepted}.")
        return mt

    def check_body(
        self, operation_id: str, content_type: str | None, body: bytes, *, max_bytes: int | None = None
    ) -> JsonValue:
        """Validate a buffered JSON body; returns the decoded value (None for an operation without a body).

        Order: size > `max_bytes` (default: the gate's `max_json_body_bytes`) -> 413 `request_too_large`; wrong
        Content-Type -> 415; empty when required, or not JSON -> 400 `schema_violation`; schema failure -> 400
        `schema_violation` with `param` = the first error's path. Streaming (multipart) bodies use
        `check_content_type` instead; calling this for them raises TypeError.
        """
        op = self._ops[operation_id]
        limit = self.max_json_body_bytes if max_bytes is None else max_bytes
        if len(body) > limit:
            raise NoemaError(ErrorCode.REQUEST_TOO_LARGE, f"Request body exceeds {limit} bytes.")
        mt = self.check_content_type(operation_id, content_type) if body or op.raw.body_required else None
        if not op.body_media_types:
            return None
        if mt is not None and mt not in JSON_MEDIA_TYPES:
            raise TypeError(f"{operation_id} takes a {mt} body; use check_content_type")
        if not body:
            if op.raw.body_required:
                raise _violation("A request body is required.", None)
            return None
        try:
            value: JsonValue = orjson.loads(body)
        except orjson.JSONDecodeError:
            raise _violation("The request body is not valid JSON.", None) from None
        assert op.body_validator is not None
        for err in op.body_validator.iter_errors(value):
            where = param_path(err.instance_path)
            raise _violation(
                f"The request body does not match the schema at {where or 'the root'} ({_keyword(err)}).", where
            )
        return value
