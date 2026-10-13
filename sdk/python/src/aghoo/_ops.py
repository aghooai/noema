"""Request building and response parsing shared by the sync and async clients."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import IO, Any, Final, Literal
from urllib.parse import quote

import httpx
from pydantic import BaseModel

from ._decision_types import DecisionResult, JevDecisionResponse
from ._events import TurnObject
from ._models import DerivativeContent, FileObject
from ._params import DecisionParams, QuestionParam, TurnParams
from ._questions import DecisionSet
from ._transport import Call

Questions = type[DecisionSet] | Sequence[QuestionParam]
TERMINAL_FILE_STATUSES: Final = frozenset({"ready", "partial", "rejected", "failed", "deleted"})


def seg(value: str) -> str:
    """A path segment (ids are percent-encoded). An empty, `.` or `..` id raises ValueError: URL normalization
    would resolve it, so an id taken from untrusted input could retarget the request to another route."""
    if value in ("", ".", ".."):
        raise ValueError(f"invalid id {value!r}: ids must not be empty, '.' or '..'")
    return quote(value, safe="")


def decision_call(questions: Questions, output_format: Literal["native", "jev"], params: DecisionParams) -> Call:
    wire = questions.questions() if isinstance(questions, type) else list(questions)
    body: dict[str, Any] = {**params, "questions": wire}
    if output_format == "jev":
        body["output_format"] = "jev"
    return Call("POST", "/v1/decisions", json=body, idempotent=True)


def parse_decision(
    questions: Questions, output_format: Literal["native", "jev"], body: Any
) -> DecisionSet | DecisionResult | JevDecisionResponse:
    if output_format == "jev":
        return JevDecisionResponse.model_validate(body)
    result = DecisionResult.model_validate(body)
    if isinstance(questions, type):
        return questions.from_result(result, body)
    return result


def turn_call(params: TurnParams, *, stream: bool) -> Call:
    return Call("POST", "/v1/turns", json={**params, "stream": stream}, idempotent=True)


def resume_call(turn_id: str, last_event_id: str) -> Call:
    return Call("GET", f"/v1/turns/{seg(turn_id)}/events", headers={"Last-Event-ID": last_event_id})


def cancel_call(turn_id: str) -> Call:
    return Call("POST", f"/v1/turns/{seg(turn_id)}/cancel", idempotent=True)


def upload_call(file: bytes | IO[bytes], filename: str, purpose: str, session_id: str | None, media_type: str) -> Call:
    """Fields first, then the file part (httpx encodes `data` before `files`): the server reads them in that order."""
    data = {"purpose": purpose} if session_id is None else {"purpose": purpose, "session_id": session_id}
    return Call("POST", "/v1/files", files={"file": (filename, file, media_type)}, data=data, idempotent=True)


def derivative_call(file_id: str, kind: str, page: int | None) -> Call:
    return Call("GET", f"/v1/files/{seg(file_id)}/derivatives/{seg(kind)}", query={"page": page}, accept="*/*")


def derivative(response: httpx.Response) -> DerivativeContent:
    media_type = response.headers.get("content-type", "application/octet-stream").split(";")[0].strip()
    return DerivativeContent(content=response.content, media_type=media_type)


def page_call(path: str, query: Mapping[str, str | int | None], cursor: str | None) -> Call:
    return Call("GET", path, query={**query, "after": cursor})


def page_items[M: BaseModel](model: type[M], body: Any) -> tuple[list[M], str | None]:
    """The page's items and the cursor of the next page (None when this is the last one)."""
    items = [model.model_validate(item) for item in body["data"]]
    cursor = body.get("next_cursor")
    return items, (cursor if body.get("has_more") and isinstance(cursor, str) else None)


def turn_object(body: Any) -> TurnObject:
    return TurnObject.model_validate(body)


def file_object(body: Any) -> FileObject:
    return FileObject.model_validate(body)
