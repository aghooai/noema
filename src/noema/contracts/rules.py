"""Semantic request rules the JSON Schema can't express, from the vendored `check_examples` (IMPL-041 point 3).

They run after the request gate accepted the body. A violation raises 400 `schema_violation` with the offending
`param` (spec-06 §3.1). The message is the rule text without client values: the vendored texts start with the
question name (`"<name>: …"`) or quote it, so `_client_free` drops those parts; `param` locates the field.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any, Final

from noema.contracts import vendor
from noema.core.errors import ErrorCode, NoemaError
from noema.core.values import JsonValue

_MAX_MESSAGE = 200
_SUBJECT_PREFIX: Final = re.compile(r"^[^\s:]+: ")  # "<question name>: " or "questions[3]: "
_QUOTED_NAME: Final = re.compile(r"^name '.*' is reserved")


def _client_free(message: str) -> str:
    """The rule text of a vendored violation without the client's question name."""
    message = _QUOTED_NAME.sub("this name is reserved", _SUBJECT_PREFIX.sub("", message, count=1), count=1)
    return message[:1].upper() + message[1:]


def _raise_first(violations: list[Any], param_prefix: str) -> None:
    if not violations:
        return
    first = violations[0]
    param = getattr(first, "param", None)
    raise NoemaError(
        ErrorCode.SCHEMA_VIOLATION,
        _client_free(str(first))[:_MAX_MESSAGE],
        param=None if param is None else param_prefix + str(param),
    )


def check_decision_request(body: Mapping[str, JsonValue], *, param_prefix: str = "") -> None:
    """Native decision rules (`check_examples.request_invariants`) on a body with `questions`: unique names, unique
    choice values / level labels, all-or-none choice groups with at least two labels. `param_prefix` qualifies the
    reported field (turns pass "decisions."). Raises `NoemaError(schema_violation)`."""
    _raise_first(list(vendor.function("check_examples", "request_invariants")(body)), param_prefix)


def check_openai_request(body: Mapping[str, JsonValue]) -> None:
    """Facade rules (`check_examples.oa_request_invariants`): unique explicit names, reserved q{i} names,
    collision-free choice identity, at most 20 input images. Raises `NoemaError(schema_violation)`."""
    _raise_first(list(vendor.function("check_examples", "oa_request_invariants")(body)), "")
