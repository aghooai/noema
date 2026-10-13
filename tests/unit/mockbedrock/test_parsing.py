"""Pure parsing helpers: image headers (the 8,000 px rule), auth (SigV4 credential scope, short-term bearer
tokens, redaction), the bounded request log (O(page) reads, count and byte bounds), quota windows and the
exception-name table."""

from __future__ import annotations

import base64
import struct
from _collections_abc import dict_keys
from collections.abc import Iterator
from typing import Any, ClassVar

import pytest
from botocore.session import get_session
from tests.unit.mockbedrock.helpers import AKID, short_term_token, sigv4_headers
from tools.mockbedrock.auth import parse_auth, redacted_headers
from tools.mockbedrock.faults import exception_as_http
from tools.mockbedrock.imagesize import image_size
from tools.mockbedrock.record import NODE_BYTES, approx_bytes
from tools.mockbedrock.state import Quota, QuotaBook, RequestLog


class CopySpy(dict[str, Any]):
    """A record that counts how often it is copied (`dict(r)` calls `keys()` once `__iter__` is overridden)."""

    copies: ClassVar[int] = 0

    def __iter__(self) -> Iterator[str]:
        return super().__iter__()

    def keys(self) -> dict_keys[str, Any]:
        CopySpy.copies += 1
        return super().keys()


def jpeg(width: int, height: int) -> bytes:
    app0 = b"\xff\xe0" + struct.pack(">H", 16) + b"JFIF\x00" + b"\x00" * 9
    sof = b"\xff\xc0" + struct.pack(">HBHHB", 11, 8, height, width, 1) + b"\x01\x11\x00"
    return b"\xff\xd8" + app0 + sof + b"\xff\xd9"


@pytest.mark.parametrize(
    ("data", "size"),
    [
        (b"\x89PNG\r\n\x1a\n\x00\x00\x00\x0dIHDR" + struct.pack(">II", 9000, 20), (9000, 20)),
        (jpeg(640, 8001), (640, 8001)),
        (b"GIF89a" + struct.pack("<HH", 7, 5) + b"\x00", (7, 5)),
        (
            b"RIFF\x00\x00\x00\x00WEBPVP8X" + b"\x00" * 8 + (99).to_bytes(3, "little") + (49).to_bytes(3, "little"),
            (100, 50),
        ),
        (
            b"RIFF\x00\x00\x00\x00WEBPVP8L\x00\x00\x00\x00\x2f" + ((9 << 14) | 19).to_bytes(4, "little") + b"\x00",
            (20, 10),
        ),
        (b"\x89PNG\r\n\x1a\n\x00\x01", None),
        (b"\xff\xd8\xff", None),
        (b"plain bytes", None),
    ],
)
def test_image_size(data: bytes, size: tuple[int, int] | None) -> None:
    assert image_size(data) == size


def test_sigv4_and_bearer_parsing() -> None:
    sig = parse_auth(sigv4_headers())
    assert (sig.scheme, sig.akid, sig.region, sig.service, sig.session_token) == (
        "sigv4",
        AKID,
        "us-east-1",
        "bedrock",
        True,
    )
    assert not parse_auth(sigv4_headers(session_token=False)).session_token
    bearer = parse_auth({"authorization": "Bearer " + short_term_token(session_token=None)})
    assert (bearer.scheme, bearer.akid, bearer.session_token) == ("bearer_short_term", AKID, False)
    assert parse_auth({"authorization": "Bearer " + short_term_token()}).session_token
    forged = "bedrock-api-key-" + base64.b64encode(b"bedrock.amazonaws.com/?Action=Other&Version=1").decode()
    assert parse_auth({"authorization": "Bearer " + forged}).scheme == "bearer_other"
    assert parse_auth({"authorization": "Bearer bedrock-api-key-%%%"}).scheme == "bearer_other"
    assert parse_auth({"authorization": "Basic abc"}).scheme == "none"
    assert parse_auth({}).akid is None
    assert redacted_headers({"authorization": "x", "x-amz-security-token": "t", "x-amz-date": "d"}) == {
        "x-amz-date": "d"
    }


def test_request_log_bounds() -> None:
    log = RequestLog(limit=2)
    for i in range(3):
        log.add({"i": i})
    assert [r["seq"] for r in log.since()] == [2, 3]
    assert log.dropped == 1
    log.clear()
    assert log.since() == []
    assert log.last_seq == 3


def test_quota_windows() -> None:
    book = QuotaBook()
    book.set([Quota("m", rpm=2, tpm=100), Quota("m", rpm=1, akid="special")])
    assert book.admit("a", "m", 10, 0.0) is None
    assert book.admit("a", "m", 95, 1.0) == "tpm"  # refused calls are not counted
    assert book.admit("a", "m", 10, 2.0) is None
    assert book.admit("a", "m", 1, 3.0) == "rpm"
    assert book.admit("a", "m", 1, 60_000.5) is None  # the first call left the window
    assert book.admit("special", "m", 1, 0.0) is None
    assert book.admit("special", "m", 1, 1.0) == "rpm"
    assert book.admit("a", "other", 10**9, 0.0) is None  # no quota for the model
    with pytest.raises(ValueError, match="model"):
        Quota.parse({"rpm": 1})


def test_request_log_reads_cost_only_the_page() -> None:
    """Regression: `since` copied (and scanned) the whole ring before slicing, so the health probe and every page
    of a full 50k ring stalled the event loop for hundreds of ms."""
    log = RequestLog(limit=50_000)
    for _ in range(50_005):
        log.add(CopySpy(outcome="ok"))
    CopySpy.copies = 0
    assert (len(log), log.dropped, log.first_seq, log.last_seq) == (50_000, 5, 6, 50_005)
    assert log.since(0, 0) == []
    assert log.since(log.last_seq) == []
    assert log.since(10**9) == []
    assert CopySpy.copies == 0
    assert [r["seq"] for r in log.since(log.last_seq - 3)] == [50_003, 50_004, 50_005]
    assert [r["seq"] for r in log.since(0, 2)] == [6, 7]  # older than the ring: from the oldest held record
    assert [r["seq"] for r in log.since(25_000, 2)] == [25_001, 25_002]
    assert CopySpy.copies == 7
    assert type(log.since(log.last_seq - 1)[0]) is dict  # plain copies, never the live record
    for after, limit in ((-1, None), (0, -2)):
        with pytest.raises(ValueError, match=">= 0"):
            log.since(after, limit)


def test_request_log_byte_budget() -> None:
    log = RequestLog(limit=100, max_bytes=1_000)
    first = log.add({}, 300)
    log.add({}, 300)
    log.charge(first, 100)
    assert (len(log), log.bytes) == (2, 700)
    log.add({}, 400)  # 1,100 > 1,000: the oldest goes
    assert ([r["seq"] for r in log.since()], log.bytes, log.dropped) == ([2, 3], 700, 1)
    log.charge(first, 10_000)  # an evicted record is no longer charged
    assert log.bytes == 700
    big = log.add({}, 5_000)  # the newest record is kept even when it alone exceeds the budget
    assert ([r["seq"] for r in log.since()], log.bytes) == ([4], 5_000)
    log.charge(big, 1)
    assert len(log) == 1
    log.clear()
    assert (len(log), log.bytes, log.since(), log.last_seq) == (0, 0, [], 4)
    with pytest.raises(ValueError, match=">= 1"):
        RequestLog(limit=0)


def test_request_log_compacts_evicted_slots() -> None:
    log = RequestLog(limit=3)
    for _ in range(5_000):
        log.add({})
    assert len(log._records) < 2_100  # evicted slots are reclaimed, not kept for the life of the mock
    assert [r["seq"] for r in log.since()] == [4_998, 4_999, 5_000]


def test_approx_bytes_counts_strings_and_nodes() -> None:
    assert approx_bytes("abc") == NODE_BYTES + 3
    assert approx_bytes({"k": ["xy", 1]}) == 5 * NODE_BYTES + 1 + 2


def test_exception_statuses_match_the_botocore_model() -> None:
    """Every Converse/ConverseStream error shape gets its service-model HTTP status (spec-10 §2.1a), not a 500."""
    model = get_session().get_service_model("bedrock-runtime")
    shapes = {s.name: s for op in ("Converse", "ConverseStream") for s in model.operation_model(op).error_shapes}
    shapes["ModelStreamErrorException"] = model.shape_for("ModelStreamErrorException")
    shapes["ServiceQuotaExceededException"] = model.shape_for("ServiceQuotaExceededException")
    assert len(shapes) == 11
    for name, shape in shapes.items():
        err = exception_as_http(name[:1].lower() + name[1:], None)
        assert (err.status, err.name) == (shape.metadata["error"]["httpStatusCode"], name), name
        assert err.message
    assert exception_as_http("modelNotReadyException", None).mantle_type == "rate_limit_error"
    assert exception_as_http("resourceNotFoundException", None).mantle_type == "not_found_error"
    unknown = exception_as_http("somethingElse", "m")
    assert (unknown.status, unknown.name, unknown.message) == (500, "SomethingElse", "m")
