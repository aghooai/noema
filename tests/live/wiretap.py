"""Wire-shape taps of the live smoke (spec-10 §2.3): the structure of what Bedrock (or the wire mock) sent, without
any value, for the mock-fidelity diff (`tests/live/fidelity.py`).

`install()` wraps four adapter internals once per process (test harness only; `noema.adapters` is unchanged):
the Converse JSON response (`converse._response_of`), the Mantle chat completion (`mantle.response_of`), and the
ConverseStream / Mantle SSE event feeds (`_ConverseRaw.feed`, `_MantleRaw.feed`), plus the raw exception of a
failed call (`AdapterBase._fail`: exception class, provider code, HTTP status, redacted message). Wave E3 adds two
shadow checks on the lean Converse path (counts and booleans only): every request's signed headers
(`converse.signed_headers`, the dict-based `_sigv4.sign_post`) against `botocore.auth.SigV4Auth` on the same
request at the same `X-Amz-Date` (`signer`), and every ConverseStream message of `EventStreamDecoder` against
botocore's `EventStreamBuffer` fed the same chunks (`decoder`). A
`CountingAdapter` opens a tap per call (`open_tap()`); the stream readers capture it when they are created, so the
task that iterates the stream does not matter. Key paths only (`usage.inputTokens`, `choices[].message.content`):
no prompt or answer text ever enters a tap.
"""

from __future__ import annotations

import datetime
import functools
from collections.abc import Callable, Mapping
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any, Final

from noema.telemetry.redact import Redactor

_CURRENT: ContextVar[Tap | None] = ContextVar("live_wiretap", default=None)
_SKIP_KEYS: Final = frozenset({"ResponseMetadata"})  # botocore's HTTP metadata, not the Bedrock body
_REDACT: Final = Redactor()
_installed = False


@dataclass
class Tap:
    """What one call put on the wire, structurally: `response` key paths, the stream's event signatures in order
    (consecutive repeats collapsed to `sig*n`), and the raw error."""

    response: list[str] = field(default_factory=list)
    events: list[str] = field(default_factory=list)
    error: dict[str, Any] | None = None
    signer: dict[str, Any] | None = None  # the last request's signer check (a connect retry signs again)
    decoder: dict[str, Any] | None = None
    _last: str | None = None
    _repeat: int = 0

    def event(self, signature: str) -> None:
        if signature == self._last:
            self._repeat += 1
            self.events[-1] = f"{signature}*{self._repeat}"
            return
        self._last, self._repeat = signature, 1
        self.events.append(signature)

    def as_json(self) -> dict[str, Any]:
        out = {"response": self.response, "events": self.events, "error": self.error}
        return out | {k: v for k, v in (("signer", self.signer), ("decoder", self.decoder)) if v is not None}


def open_tap() -> Tap:
    """A fresh tap for the current call (context-local)."""
    tap = Tap()
    _CURRENT.set(tap)
    return tap


def key_paths(obj: object, prefix: str = "") -> list[str]:
    """Sorted key paths of a JSON-like value with leaf types (`a.b:str`, `a[].c:int`); lists merge their items."""
    out: set[str] = set()
    if isinstance(obj, Mapping):
        for key, value in obj.items():
            if key not in _SKIP_KEYS:
                out.update(key_paths(value, f"{prefix}.{key}" if prefix else str(key)))
    elif isinstance(obj, list | tuple):
        for item in obj:
            out.update(key_paths(item, f"{prefix}[]"))
        if not obj:
            out.add(f"{prefix}[]:empty")
    else:
        out.add(f"{prefix}:{type(obj).__name__}")
    return sorted(out)


def _signature(raw: object) -> str:
    """One stream event's structure: Converse `contentBlockDelta.delta.text`; Mantle `chunk{...}`."""
    if not isinstance(raw, Mapping):
        return f"<{type(raw).__name__}>"
    if "choices" in raw or "object" in raw:  # a Mantle chat.completion.chunk
        return "chunk{" + ",".join(p.split(":")[0] for p in key_paths(raw) if not p.startswith(("id", "created"))) + "}"
    return "|".join(sorted(p.rsplit(":", 1)[0] for p in key_paths(raw)))


def _wrap_response(fn: Callable[..., Any]) -> Callable[..., Any]:
    @functools.wraps(fn)
    def wrapper(data: object, *args: Any, **kwargs: Any) -> Any:
        tap = _CURRENT.get()
        if tap is not None:
            tap.response = key_paths(data)
        return fn(data, *args, **kwargs)

    return wrapper


def _wrap_reader(cls: type[Any]) -> None:
    init, feed = cls.__init__, cls.feed

    @functools.wraps(init)
    def new_init(self: Any, *args: Any, **kwargs: Any) -> None:
        init(self, *args, **kwargs)
        self._live_tap = _CURRENT.get()
        if self._live_tap is not None and hasattr(self, "_decoder"):  # ConverseStream (Wave E3 decoder shadow)
            self._decoder = _ShadowDecoder(self._decoder, self._live_tap)

    @functools.wraps(feed)
    def new_feed(self: Any, raw: object) -> Any:
        tap: Tap | None = getattr(self, "_live_tap", None)
        if tap is not None:
            tap.event(_signature(raw))
        return feed(self, raw)

    cls.__init__, cls.feed = new_init, new_feed


_SDK_UNSIGNED: Final = frozenset({"User-Agent", "amz-sdk-invocation-id", "amz-sdk-request"})


def _botocore_signed(creds: Any, url: str, body: bytes, headers: Mapping[str, str]) -> list[tuple[str, str]]:
    """The headers `SigV4Auth.add_auth` gives the same request at the lean signer's `X-Amz-Date`."""
    from botocore import auth
    from botocore.awsrequest import AWSRequest
    from botocore.credentials import ReadOnlyCredentials

    from noema.adapters._converse_wire import SIGNING_NAME

    when = datetime.datetime.strptime(headers["X-Amz-Date"], "%Y%m%dT%H%M%SZ").replace(tzinfo=datetime.UTC)
    token = creds.aws.session_token
    keys = ReadOnlyCredentials(
        creds.aws.access_key_id,
        creds.aws.secret_access_key.get_secret_value(),
        token.get_secret_value() if token is not None else None,
    )
    request = AWSRequest(method="POST", url=url, data=body, headers={"Content-Type": headers["Content-Type"]})
    clock: Any = auth  # botocore's signing clock (not in the stubs)
    saved = clock.get_current_datetime
    clock.get_current_datetime = lambda *a, **k: when  # synchronous: no other signing interleaves
    try:
        auth.SigV4Auth(keys, SIGNING_NAME, creds.region).add_auth(request)
    finally:
        clock.get_current_datetime = saved
    return [(k, str(v)) for k, v in request.headers.items()]


def _wrap_signer(fn: Callable[..., dict[str, str]]) -> Callable[..., dict[str, str]]:
    @functools.wraps(fn)
    def wrapper(creds: Any, url: str, body: bytes) -> dict[str, str]:
        headers = fn(creds, url, body)
        tap = _CURRENT.get()
        if tap is not None:
            expected = _botocore_signed(creds, url, body, headers)
            lean = [(k, v) for k, v in headers.items() if k not in _SDK_UNSIGNED]
            tap.signer = {
                "equal_to_botocore": lean == expected,
                "signed": sorted(k.lower() for k, _ in expected),
                "model_path_colon_encoded": "%3A" in url,
                "session_token": "X-Amz-Security-Token" in headers,
            }
        return headers

    return wrapper


class _ShadowDecoder:
    """noema's `EventStreamDecoder`, with botocore's `EventStreamBuffer` fed the same chunks: each message noema
    yields is compared with botocore's next one (headers and payload); only counts reach the tap."""

    def __init__(self, inner: Any, tap: Tap) -> None:
        from botocore.eventstream import EventStreamBuffer

        self._inner, self._shadow = inner, EventStreamBuffer()
        self._stats = tap.decoder = {"chunks": 0, "bytes": 0, "messages": 0, "equal": 0, "botocore_extra": 0,
                                     "botocore_errors": 0, "fallback": False}  # fmt: skip

    def add_data(self, data: bytes) -> None:
        self._stats["chunks"] += 1
        self._stats["bytes"] += len(data)
        self._shadow.add_data(data)
        self._inner.add_data(data)

    def _botocore_next(self) -> tuple[dict[str, Any], bytes] | None:
        try:
            ref: Any = next(self._shadow)
        except StopIteration:
            return None
        except Exception:
            self._stats["botocore_errors"] += 1
            return None
        return dict(ref.headers), ref.payload

    def next(self) -> Any:
        message = self._inner.next()
        self._stats["fallback"] = self._stats["fallback"] or getattr(self._inner, "_fallback", None) is not None
        ref = self._botocore_next()
        if message is None:
            self._stats["botocore_extra"] += ref is not None
            return None
        self._stats["messages"] += 1
        self._stats["equal"] += ref == (dict(message[0]), message[1])
        return message


def _wrap_fail(fn: Callable[..., Any]) -> Callable[..., Any]:
    @functools.wraps(fn)
    def wrapper(self: Any, exc: BaseException, *args: Any, **kwargs: Any) -> Any:
        tap = _CURRENT.get()
        if tap is not None and tap.error is None:
            err = getattr(exc, "response", None)
            body = err.get("Error", {}) if isinstance(err, Mapping) else {}
            meta = err.get("ResponseMetadata", {}) if isinstance(err, Mapping) else {}
            tap.error = {
                "exception": type(exc).__name__,
                "code": body.get("Code") or getattr(exc, "status", None),
                "http_status": meta.get("HTTPStatusCode") or getattr(exc, "status", None),
                "message": _REDACT.redact(str(body.get("Message") or exc))[:400],
            }
        return fn(self, exc, *args, **kwargs)

    return wrapper


def install() -> None:
    """Install the taps once per process (idempotent)."""
    global _installed
    if _installed:
        return
    from noema.adapters import _base, converse, mantle

    converse._response_of = _wrap_response(converse._response_of)
    converse.signed_headers = _wrap_signer(converse.signed_headers)  # type: ignore[attr-defined]
    mantle.response_of = _wrap_response(mantle.response_of)
    _wrap_reader(converse._ConverseRaw)
    _wrap_reader(mantle._MantleRaw)
    _base.AdapterBase._fail = _wrap_fail(_base.AdapterBase._fail)  # type: ignore[method-assign]
    _installed = True
