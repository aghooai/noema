"""Step 5: the request gate (IMPL-041, IMPL-064, IMPL-037; spec-04 §1).

`RequestGate` (jsonschema-rs) is the only accept/reject authority: declared parameters are coerced and validated
(unknown query parameters are 400), the JSON body is checked against its schema, then the semantic rules the schema
can't express run (`request_invariants` / `oa_request_invariants`), then the arithmetic inline-image size check
(413 `image_too_large` above 3,750,000 decoded bytes, computed from the base64 length without decoding).

A malformed path parameter on an operation that declares no 400 is that operation's 404 code (`not_found`, or
`session_not_found` for session ids): a malformed id is an unknown id (IMPL-030). Wave E2 (ruling R7): so is a
path id holding a control character (U+0000-U+001F, U+007F-U+009F; Postgres text cannot hold U+0000, whose lookup
answered 500), and a query string parameter holding one is 400 `schema_violation`; neither reaches the route.
"""

from __future__ import annotations

import re
import urllib.parse
from collections.abc import Callable, Mapping
from typing import Final

from noema.contracts import rules
from noema.contracts.bundle import RawOperation
from noema.contracts.gate import JSON_MEDIA_TYPES, RequestGate
from noema.core.errors import ErrorCode, NoemaError
from noema.core.values import JsonValue

INLINE_IMAGE_MAX_BYTES: Final = 3_750_000  # decoded bytes (IMPL-037)
DEADLINE_PARAM: Final = "Aghoo-Deadline-Ms"
MAX_QUERY_FIELDS: Final = 64
_CONTROL: Final = re.compile("[\x00-\x1f\x7f-\x9f]")


def parse_query(query_string: bytes) -> dict[str, list[str]]:
    """Decode a raw query string; more than `MAX_QUERY_FIELDS` fields is 400 `schema_violation`."""
    try:
        return urllib.parse.parse_qs(
            query_string.decode("latin-1"), keep_blank_values=True, max_num_fields=MAX_QUERY_FIELDS
        )
    except ValueError:
        raise NoemaError(ErrorCode.SCHEMA_VIOLATION, "Too many query parameters.") from None


def _not_found_for(op: RawOperation, param: str) -> NoemaError:
    if param == "session_id" and ErrorCode.SESSION_NOT_FOUND.value in op.error_codes:
        return NoemaError(ErrorCode.SESSION_NOT_FOUND, "Session not found.")
    return NoemaError(ErrorCode.NOT_FOUND, "Not found.")


def check_params(
    gate: RequestGate,
    op: RawOperation,
    *,
    path: Mapping[str, str],
    query: Mapping[str, list[str]],
    headers: Mapping[str, str],
) -> dict[str, JsonValue]:
    """`RequestGate.check_params`, with malformed path ids mapped to 404: an id that cannot match its pattern or holds
    a control character is an unknown id (IMPL-030, R7). Other malformed path parameters (a derivative `kind`) stay
    400 (IMPL-065); a query string parameter holding a control character is 400."""
    try:
        params = gate.check_params(op.operation_id, path=path, query=query, headers=headers)
    except NoemaError as err:
        if err.param in path and (err.param or "").endswith("_id"):
            raise _not_found_for(op, err.param or "") from None
        raise
    for name, value in path.items():
        if name.endswith("_id") and _CONTROL.search(value):
            raise _not_found_for(op, name)
    for name, values in query.items():
        if any(_CONTROL.search(v) for v in values):
            raise NoemaError(ErrorCode.SCHEMA_VIOLATION, f"{name} holds a control character.", param=name)
    return params


def is_json_operation(op: RawOperation) -> bool:
    """True when the operation takes a JSON request body (buffered and capped by the middleware)."""
    return any(mt in JSON_MEDIA_TYPES for mt in op.body_ptrs)


def _turn_rules(body: Mapping[str, JsonValue]) -> None:
    decisions = body.get("decisions")
    if isinstance(decisions, Mapping) and "questions" in decisions:
        rules.check_decision_request(decisions, param_prefix="decisions.")


def _session_rules(body: Mapping[str, JsonValue]) -> None:
    """Session metadata is stored as is: U+0000, which Postgres text cannot hold, is 400 (Wave E2, finding 17)."""
    metadata = body.get("metadata")
    if not isinstance(metadata, Mapping):
        return
    for k, v in metadata.items():
        if "\x00" in k or (isinstance(v, str) and "\x00" in v):
            param = "metadata" if "\x00" in k else f"metadata.{k}"
            raise NoemaError(ErrorCode.SCHEMA_VIOLATION, "metadata cannot hold U+0000.", param=param)


SEMANTIC_RULES: Final[Mapping[str, Callable[[Mapping[str, JsonValue]], None]]] = {
    "createTurn": _turn_rules,
    "createSession": _session_rules,
    "updateSession": _session_rules,
    "createDecision": rules.check_decision_request,
    "createDecisionOpenAICompat": rules.check_openai_request,
}
INLINE_IMAGE_OPERATIONS: Final = frozenset({"createTurn", "createDecision", "createDecisionOpenAICompat"})


def decoded_size(data_url: str) -> int:
    """Decoded byte count of a base64 data URL, by arithmetic: `len(b64) * 3 // 4 - padding` (IMPL-037)."""
    b64 = data_url.partition(",")[2]
    padding = 2 if b64.endswith("==") else 1 if b64.endswith("=") else 0
    return max(0, len(b64) * 3 // 4 - padding)


def check_inline_images(body: Mapping[str, JsonValue]) -> None:
    """413 `image_too_large` for the first `input[i].content[j]` image part over `INLINE_IMAGE_MAX_BYTES`."""
    messages = body.get("input")
    if not isinstance(messages, list):
        return
    for i, message in enumerate(messages):
        content = message.get("content") if isinstance(message, Mapping) else None
        if not isinstance(content, list):
            continue
        for j, part in enumerate(content):
            if not isinstance(part, Mapping) or part.get("type") != "input_image":
                continue
            url = part.get("image_url")
            if isinstance(url, str) and decoded_size(url) > INLINE_IMAGE_MAX_BYTES:
                raise NoemaError(
                    ErrorCode.IMAGE_TOO_LARGE,
                    f"Inline image exceeds {INLINE_IMAGE_MAX_BYTES} bytes.",
                    param=f"input[{i}].content[{j}].image_url",
                )


def check_body(gate: RequestGate, op: RawOperation, content_type: str | None, body: bytes, limit: int) -> JsonValue:
    """Schema-check a buffered JSON body, then the operation's semantic rules and the inline-image sizes."""
    value = gate.check_body(op.operation_id, content_type, body, max_bytes=limit)
    if isinstance(value, Mapping):
        rule = SEMANTIC_RULES.get(op.operation_id)
        if rule is not None:
            rule(value)
        if op.operation_id in INLINE_IMAGE_OPERATIONS:
            check_inline_images(value)
    return value
