"""Request records (`GET /_mock/requests`, spec-10 §2.1 and §5 "Stamps").

One JSON-ready dict per request: `seq`, `method`, `path`, `op`, `model`, `headers` (credentials removed), `auth`
(scheme, AKID, region, service, session token present), `akid`, `body` (parsed; every `bytes` string replaced by
`{"$bytes_len", "sha256"}`), `request_id` (x-amzn-RequestId / x-request-id), `t_req_recv_ns` (CLOCK_MONOTONIC
when the last request byte arrived), `at_ms` (ms since the scenario load), `nonce` (from a `«nonce:<id>»` marker
in the prompt), `rule`, `k`, `fault`, `barrier`, `status`, `error`, `t_first_byte_ns`, `t_first_text_sent_ns`,
`t_done_ns` and `outcome` (`ok`, `rejected`, `fault`, `throttled_quota`, `unmatched`, `hang`, `reset`,
`disconnected`).

Text is kept in full (the SE6-SE8 capture tests scan every prompt); the log bounds memory by an approximate byte
size per record (`approx_bytes`: string lengths plus a per-node overhead) as well as by count.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import re
from collections.abc import Mapping
from typing import Any, Final

from .auth import AuthInfo, redacted_headers
from .httpserver import Request
from .state import MockState

NONCE: Final = re.compile(r"«nonce:([^»]{1,128})»")
NODE_BYTES: Final = 64  # rough CPython cost of one JSON node (object header, dict slot or list slot)
RECORD_BYTES: Final = 2048  # the record dict with its fixed fields and the auth summary


def _blob_summary(value: str) -> dict[str, object]:
    try:
        data = base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError):
        return {"$bytes_len": None, "b64_len": len(value)}
    return {"$bytes_len": len(data), "sha256": hashlib.sha256(data).hexdigest()}


def summarize(node: Any) -> Any:
    """A copy of a parsed body with every `bytes` string (base64 on the wire) replaced by its length and digest."""
    if isinstance(node, Mapping):
        return {k: _blob_summary(v) if k == "bytes" and isinstance(v, str) else summarize(v) for k, v in node.items()}
    if isinstance(node, list):
        return [summarize(v) for v in node]
    return node


def approx_bytes(node: Any) -> int:
    """The approximate memory a parsed JSON value retains: string lengths plus `NODE_BYTES` per node."""
    if isinstance(node, str):
        return NODE_BYTES + len(node)
    if isinstance(node, Mapping):
        return NODE_BYTES + sum(approx_bytes(k) + approx_bytes(v) for k, v in node.items())
    if isinstance(node, list):
        return NODE_BYTES + sum(approx_bytes(v) for v in node)
    return NODE_BYTES


def set_body(state: MockState, rec: dict[str, Any], body: Any) -> None:
    """Store the summarized request body in its record and charge its size to the log's byte budget."""
    summary = summarize(body)
    rec["body"] = summary
    state.log.charge(rec, approx_bytes(summary))


def new_record(state: MockState, req: Request, op: str | None, model: str | None, auth: AuthInfo) -> dict[str, Any]:
    """Start the record of a model request (added to the log at once, so held requests are visible)."""
    headers = redacted_headers(req.headers)
    return state.log.add(
        {
            "method": req.method,
            "path": req.path,
            "op": op,
            "model": model,
            "headers": headers,
            "auth": auth.as_record(),
            "akid": auth.akid,
            "body": None,
            "t_req_recv_ns": req.received_ns,
            "at_ms": round((req.received_ns - state.t0_ns) / 1e6, 3),
            "request_id": None,
            "nonce": None,
            "rule": None,
            "k": None,
            "fault": None,
            "barrier": None,
            "status": None,
            "error": None,
            "t_first_byte_ns": None,
            "t_first_text_sent_ns": None,
            "t_done_ns": None,
            "outcome": None,
        },
        RECORD_BYTES + approx_bytes(headers),
    )


def find_nonce(prompt: str) -> str | None:
    match = NONCE.search(prompt)
    return None if match is None else match.group(1)
