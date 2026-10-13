"""Step 1: the request id (IMPL-033, spec-08 §4).

A valid client `x-request-id` (1-128 characters of `[A-Za-z0-9._:-]`) is echoed; any other value, or none, is
replaced by `req_<ULID>`. The id is never rejected and is returned on every response (`x-request-id`, and
`error.request_id` in every envelope).
"""

from __future__ import annotations

import re
from typing import Final

from noema.core.ids import IdGen

REQUEST_ID_HEADER: Final = "x-request-id"
_VALID: Final = re.compile(r"[A-Za-z0-9._:-]{1,128}")


def is_valid_request_id(value: str) -> bool:
    """True for 1-128 characters of `[A-Za-z0-9._:-]`."""
    return _VALID.fullmatch(value) is not None


def resolve_request_id(client_value: str | None, ids: IdGen) -> str:
    """The client's id when valid, else a fresh `req_<ULID>`."""
    if client_value is not None and is_valid_request_id(client_value):
        return client_value
    return ids.new("req")
