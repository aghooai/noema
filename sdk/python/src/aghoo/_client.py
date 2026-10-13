"""The synchronous client `Aghoo`."""

from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence
from typing import IO, Literal, Self, Unpack, overload

import httpx
from pydantic import BaseModel

from ._decision_types import DecisionResult, JevDecisionResponse
from ._errors import AghooConnectionError
from ._events import TurnObject
from ._models import (
    CalibratorInfo,
    CancelResult,
    DerivativeContent,
    FileDeleted,
    FileObject,
    LabelingDroppedItem,
    LabelingRun,
    ProfileInfo,
    Session,
    SessionDeleted,
    SessionTurn,
    UsageRow,
)
from ._ops import (
    TERMINAL_FILE_STATUSES,
    Questions,
    cancel_call,
    decision_call,
    derivative,
    derivative_call,
    file_object,
    page_call,
    page_items,
    parse_decision,
    resume_call,
    seg,
    turn_call,
    turn_object,
    upload_call,
)
from ._params import DecisionParams, QuestionParam, RequestOptions, SessionCreateParams, SessionUpdateParams, TurnParams
from ._questions import DecisionSet
from ._retry import DEFAULT_MAX_RETRIES
from ._stream import TurnStream
from ._stream import event_json as _event_json
from ._transport import DEFAULT_TIMEOUT_S, Call, Config, SyncTransport


def _pages[M: BaseModel](
    t: SyncTransport,
    path: str,
    query: Mapping[str, str | int | None],
    model: type[M],
    opts: RequestOptions | None,
    after: str | None = None,
) -> Iterator[M]:
    cursor = after
    while True:
        items, cursor = page_items(model, t.json(page_call(path, query, cursor), opts or {}))
        yield from items
        if cursor is None:
            return


class Decisions:
    """Typed decisions: `POST /v1/decisions`, `GET /v1/decisions/{id}`."""

    def __init__(self, t: SyncTransport) -> None:
        self._t = t

    @overload
    def create[DS: DecisionSet](
        self,
        questions: type[DS],
        /,
        *,
        output_format: Literal["native"] = ...,
        options: RequestOptions | None = ...,
        **params: Unpack[DecisionParams],
    ) -> DS: ...
    @overload
    def create(
        self,
        questions: Sequence[QuestionParam],
        /,
        *,
        output_format: Literal["native"] = ...,
        options: RequestOptions | None = ...,
        **params: Unpack[DecisionParams],
    ) -> DecisionResult: ...
    @overload
    def create(
        self,
        questions: Questions,
        /,
        *,
        output_format: Literal["jev"],
        options: RequestOptions | None = ...,
        **params: Unpack[DecisionParams],
    ) -> JevDecisionResponse: ...
    def create(
        self,
        questions: Questions,
        /,
        *,
        output_format: Literal["native", "jev"] = "native",
        options: RequestOptions | None = None,
        **params: Unpack[DecisionParams],
    ) -> DecisionSet | DecisionResult | JevDecisionResponse:
        """A DecisionSet subclass gives an instance with typed answers per field (and `.result`); a list of question
        dicts gives the DecisionResult; `output_format="jev"` gives the Jev-shaped response."""
        body = self._t.json(decision_call(questions, output_format, params), options or {})
        return parse_decision(questions, output_format, body)

    def retrieve(self, decision_id: str, *, options: RequestOptions | None = None) -> DecisionResult:
        """A stored decision, including late member merges."""
        return DecisionResult.model_validate(
            self._t.json(Call("GET", f"/v1/decisions/{seg(decision_id)}"), options or {})
        )


class Turns:
    """Smart turns: streams, non-streaming turns, retrieval and explicit cancel."""

    def __init__(self, t: SyncTransport) -> None:
        self._t = t

    def stream(
        self,
        *,
        options: RequestOptions | None = None,
        last_event_id: str | None = None,
        turn_id: str | None = None,
        **params: Unpack[TurnParams],
    ) -> TurnStream:
        """A streamed turn (`stream: true`); the request starts on `__enter__` or the first event. With
        `last_event_id` and `turn_id` the SDK resumes that turn (Phase 2; Phase 1 servers answer 501)."""
        opts = options or {}
        if last_event_id is not None:
            if turn_id is None:
                raise TypeError("resuming with last_event_id needs the turn_id it belongs to")
            resume = resume_call(turn_id, last_event_id)
            return TurnStream(lambda stop: self._t.sse(resume, opts, stop), self.cancel, turn_id)
        call = turn_call(params, stream=True)
        return TurnStream(lambda stop: self._t.sse(call, opts, stop), self.cancel)

    def create(self, *, options: RequestOptions | None = None, **params: Unpack[TurnParams]) -> TurnObject:
        """A non-streaming turn (`stream: false`): the reconciled TurnObject."""
        return turn_object(self._t.json(turn_call(params, stream=False), options or {}))

    def retrieve(self, turn_id: str, *, options: RequestOptions | None = None) -> TurnObject:
        return turn_object(self._t.json(Call("GET", f"/v1/turns/{seg(turn_id)}"), options or {}))

    def cancel(self, turn_id: str, *, options: RequestOptions | None = None) -> CancelResult:
        """Idempotent explicit cancel; keeps committed partial text."""
        return CancelResult.model_validate(self._t.json(cancel_call(turn_id), options or {}))


class Profiles:
    """Decision and generation profiles the caller may select."""

    def __init__(self, t: SyncTransport) -> None:
        self._t = t

    def list(
        self, *, kind: Literal["decision", "generation"] | None = None, options: RequestOptions | None = None
    ) -> list[ProfileInfo]:
        """Profiles the caller may select (tenant allowlist applied)."""
        body = self._t.json(Call("GET", "/v1/profiles", query={"kind": kind}), options or {})
        return [ProfileInfo.model_validate(p) for p in body["data"]]


class Sessions:
    """Sessions and their authorized history."""

    def __init__(self, t: SyncTransport) -> None:
        self._t = t

    def create(self, *, options: RequestOptions | None = None, **params: Unpack[SessionCreateParams]) -> Session:
        call = Call("POST", "/v1/sessions", json=dict(params), idempotent=True)
        return Session.model_validate(self._t.json(call, options or {}))

    def retrieve(self, session_id: str, *, options: RequestOptions | None = None) -> Session:
        return Session.model_validate(self._t.json(Call("GET", f"/v1/sessions/{seg(session_id)}"), options or {}))

    def list(self, *, limit: int | None = None, options: RequestOptions | None = None) -> Iterator[Session]:
        """Newest first; pages are fetched lazily."""
        return _pages(self._t, "/v1/sessions", {"limit": limit}, Session, options)

    def update(
        self, session_id: str, *, options: RequestOptions | None = None, **params: Unpack[SessionUpdateParams]
    ) -> Session:
        """PATCH semantics: present fields replace the stored value; `metadata` replaces the whole map."""
        call = Call("PATCH", f"/v1/sessions/{seg(session_id)}", json=dict(params))
        return Session.model_validate(self._t.json(call, options or {}))

    def turns(
        self,
        session_id: str,
        *,
        limit: int | None = None,
        after: str | None = None,
        options: RequestOptions | None = None,
    ) -> Iterator[SessionTurn]:
        """Oldest first: each committed turn yields its user message, then its assistant message."""
        path = f"/v1/sessions/{seg(session_id)}/turns"
        return _pages(self._t, path, {"limit": limit}, SessionTurn, options, after)

    def delete(self, session_id: str, *, options: RequestOptions | None = None) -> SessionDeleted:
        """Cascading, asynchronous deletion."""
        body = self._t.json(Call("DELETE", f"/v1/sessions/{seg(session_id)}"), options or {})
        return SessionDeleted.model_validate(body)


class Files:
    """Uploads (validated and extracted asynchronously), status, derivatives and status streams."""

    def __init__(self, t: SyncTransport) -> None:
        self._t = t

    def upload(
        self,
        file: bytes | IO[bytes],
        *,
        filename: str,
        purpose: Literal["input"] = "input",
        session_id: str | None = None,
        media_type: str = "application/octet-stream",
        options: RequestOptions | None = None,
    ) -> FileObject:
        """Multipart upload (validated and extracted asynchronously; answered 202). The type is sniffed server-side.
        A seekable file is streamed in chunks and sent from offset 0 on every attempt; another file object is read
        into memory first (so a retry can resend it)."""
        content = file if isinstance(file, bytes) or file.seekable() else file.read()
        call = upload_call(content, filename, purpose, session_id, media_type)
        return file_object(self._t.json(call, options or {}))

    def retrieve(self, file_id: str, *, options: RequestOptions | None = None) -> FileObject:
        return file_object(self._t.json(Call("GET", f"/v1/files/{seg(file_id)}"), options or {}))

    def watch(self, file_id: str, *, options: RequestOptions | None = None) -> Iterator[FileObject]:
        """`file.updated` snapshots, starting with the current one, until ready / partial / rejected / failed /
        deleted. The request starts on the first iteration."""
        response, messages = self._t.sse(Call("GET", f"/v1/files/{seg(file_id)}/events"), options or {})
        try:
            for message in messages:
                if message.event != "file.updated":
                    continue
                snapshot = file_object(_event_json(message.data))
                yield snapshot
                if snapshot.status in TERMINAL_FILE_STATUSES:
                    return
        finally:
            response.close()
        raise AghooConnectionError("file event stream ended before a terminal status")

    def delete(self, file_id: str, *, options: RequestOptions | None = None) -> FileDeleted:
        return FileDeleted.model_validate(self._t.json(Call("DELETE", f"/v1/files/{seg(file_id)}"), options or {}))

    def derivative(
        self, file_id: str, kind: str, *, page: int | None = None, options: RequestOptions | None = None
    ) -> DerivativeContent:
        """Derivative bytes and content type; `page` (1-based) is required for page_images."""
        return derivative(self._t.response(derivative_call(file_id, kind, page), options or {}))


class Usage:
    """Usage and cost rows."""

    def __init__(self, t: SyncTransport) -> None:
        self._t = t

    def list(
        self,
        *,
        group_by: Literal["project", "session", "model", "day", "line"] | None = None,
        from_: str | None = None,
        to: str | None = None,
        limit: int | None = None,
        options: RequestOptions | None = None,
    ) -> Iterator[UsageRow]:
        """Rows ordered by key, then line; defaults: group_by "line", the last 30 UTC days (dates are YYYY-MM-DD)."""
        query = {"group_by": group_by, "from": from_, "to": to, "limit": limit}
        return _pages(self._t, "/v1/usage", query, UsageRow, options)


class Calibrators:
    """Calibrator artifacts with holdout metrics and reliability bins (read-only, W4)."""

    def __init__(self, t: SyncTransport) -> None:
        self._t = t

    def list(
        self,
        *,
        profile_id: str | None = None,
        question_name: str | None = None,
        status: str | None = None,
        limit: int | None = None,
        options: RequestOptions | None = None,
    ) -> Iterator[CalibratorInfo]:
        query = {"profile_id": profile_id, "question_name": question_name, "status": status, "limit": limit}
        return _pages(self._t, "/v1/admin/calibrators", query, CalibratorInfo, options)


class Labeling:
    """LLM-consensus labeling runs and the items the labelers did not agree on (read-only, W4)."""

    def __init__(self, t: SyncTransport) -> None:
        self._t = t

    def runs(
        self,
        *,
        profile_id: str | None = None,
        question_name: str | None = None,
        status: str | None = None,
        limit: int | None = None,
        options: RequestOptions | None = None,
    ) -> Iterator[LabelingRun]:
        query = {"profile_id": profile_id, "question_name": question_name, "status": status, "limit": limit}
        return _pages(self._t, "/v1/admin/labeling/runs", query, LabelingRun, options)

    def dropped(
        self,
        *,
        run_id: str | None = None,
        question_name: str | None = None,
        reason: str | None = None,
        limit: int | None = None,
        options: RequestOptions | None = None,
    ) -> Iterator[LabelingDroppedItem]:
        query = {"run_id": run_id, "question_name": question_name, "reason": reason, "limit": limit}
        return _pages(self._t, "/v1/admin/labeling/dropped", query, LabelingDroppedItem, options)


class Admin:
    """Read-only W4 views; other roles get AghooError code permission_denied."""

    def __init__(self, t: SyncTransport) -> None:
        self.calibrators = Calibrators(t)
        self.labeling = Labeling(t)


class Aghoo:
    """Synchronous client for the Noema API (every model call behind it runs on Amazon Bedrock).

    Non-2xx responses raise AghooError after retries (connect errors, timeouts, retryable codes; full jitter, base
    0.5 s, cap 8 s, `retry-after` honored; one Idempotency-Key per POST across its retries). `timeout` is httpx's
    per-operation timeout in seconds. Pass `http_client` to reuse a configured httpx.Client (it is not closed here).
    """

    def __init__(
        self,
        *,
        api_key: str | None = None,
        base_url: str | None = None,
        timeout: float = DEFAULT_TIMEOUT_S,
        max_retries: int = DEFAULT_MAX_RETRIES,
        http_client: httpx.Client | None = None,
    ) -> None:
        self._owns_http = http_client is None
        self._http = http_client or httpx.Client()
        t = SyncTransport(Config(api_key, base_url, timeout, max_retries), self._http)
        self.decisions = Decisions(t)
        self.turns = Turns(t)
        self.profiles = Profiles(t)
        self.sessions = Sessions(t)
        self.files = Files(t)
        self.usage = Usage(t)
        self.admin = Admin(t)

    def close(self) -> None:
        if self._owns_http:
            self._http.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
