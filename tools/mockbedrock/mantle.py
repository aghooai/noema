"""Mantle, the OpenAI-compatible Bedrock surface (spec-10 §2.1a): `POST {base}/chat/completions` as JSON
(`stream: false`, decision members, `logprobs`/`top_logprobs`) or SSE (`stream: true`; usage only with
`stream_options.include_usage`; `data: [DONE]`), plus the route quirks of the probes: Claude and Luna rejected on
chat completions, Luna answering only on `/openai/v1/responses` (non-streaming), `/v1/decisions` rejecting every
model. Auth is a short-term bearer token or SigV4, as the scenario's `mantle_auth` says (ADR-001 §6)."""

from __future__ import annotations

import math
import time
from collections.abc import Mapping
from typing import Any, Final, Literal

import orjson
from tests.fakes.scenario import Completion, StreamPlan

from .auth import parse_auth
from .common import STOP_TO_FINISH, pad_member, reasoning_text, stamp
from .eventstream import exception_type_of
from .faults import REFUSAL_TEXT, WireError, exception_as_http
from .httpserver import Request, Responder
from .models import DECISIONS_REJECT, MantleReject
from .pipeline import Call, Pacer, run
from .record import find_nonce, new_record, set_body
from .state import MockState
from .validate import ChatFacts, MantleInvalid, check_chat, check_responses

_JSON: Final = ("content-type", "application/json")
_SSE: Final = (("content-type", "text/event-stream; charset=utf-8"), ("cache-control", "no-cache"))
Route = Literal["chat", "responses", "openai_responses", "decisions"]


def error_payload(message: str, type_: str, code: str | None, param: str | None = None) -> dict[str, Any]:
    return {"error": {"message": message, "type": type_, "code": code, "param": param}}


def error_body(message: str, type_: str, code: str | None, param: str | None = None) -> bytes:
    return orjson.dumps(error_payload(message, type_, code, param))


def mantle_usage(usage: Mapping[str, int]) -> dict[str, Any]:
    """Chat `usage`: prompt_tokens include cached and cache-write tokens (OpenAI semantics)."""
    read, write = usage.get("cached_input_tokens", 0), usage.get("cache_write_input_tokens", 0)
    prompt = usage.get("input_tokens", 0) + read + write
    completion = usage.get("output_tokens", 0)
    body: dict[str, Any] = {
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "total_tokens": prompt + completion,
    }
    if read:
        body["prompt_tokens_details"] = {"cached_tokens": read}
    if usage.get("reasoning_tokens", 0):
        body["completion_tokens_details"] = {"reasoning_tokens": usage["reasoning_tokens"]}
    return body


def _token(token: str, logprob: float) -> dict[str, Any]:
    return {"token": token, "logprob": logprob, "bytes": list(token.encode())}


def chat_logprobs(comp: Completion, top_logprobs: int | None) -> dict[str, Any]:
    """`choices[0].logprobs`: the answer token with its top list (scenario entries, most probable first); a
    verbatim answer gets one certain token."""
    entries = [(t, p) for t, p in (comp.logprobs or ()) if p > 0]
    if not entries:
        if not comp.text:
            return {"content": []}
        entries = [(comp.text, 1.0)]
    top = [_token(t, math.log(p)) for t, p in entries[: top_logprobs or 0]]
    chosen = _token(entries[0][0], math.log(entries[0][1]))
    return {"content": [{**chosen, "top_logprobs": top}], "refusal": None}


def auth_error(expected: str | None, scheme: str) -> WireError:
    want = expected or "bearer_short_term or sigv4"
    return WireError(
        401,
        "UnrecognizedClientException",
        f"Invalid authentication credentials: this endpoint accepts {want}, the request used {scheme}.",
        "authentication_error",
        "invalid_api_key",
    )


class MantleWire:
    """Validates and answers one Mantle route for one mock instance."""

    def __init__(self, state: MockState, route: Route) -> None:
        self.state = state
        self.route: Route = route

    async def handle(self, req: Request, resp: Responder) -> None:
        auth = parse_auth(req.headers)
        rec = new_record(self.state, req, None, None, auth)
        call = Call(req, resp, rec, "chat", "", "", {}, auth.akid)
        if not await self._prepare(call):
            rec["t_done_ns"] = self.state.clock.now_ns()
            return
        await run(self.state, call, self)

    def _auth_error(self, call: Call) -> WireError | None:
        """The 401 when the request's auth does not match the scenario's `mantle_auth` (either mode without one)."""
        auth = call.rec["auth"]
        bearer = auth["scheme"] == "bearer_short_term" and call.akid is not None
        sigv4 = auth["scheme"] == "sigv4" and call.akid is not None and bool(auth["session_token"])
        expected = None if self.state.scenario is None else self.state.scenario.mantle_auth
        ok = bearer if expected == "bearer_short_term" else sigv4 if expected == "sigv4" else bearer or sigv4
        return None if ok else auth_error(expected, auth["scheme"])

    async def _prepare(self, call: Call) -> bool:
        error = self._auth_error(call)
        if error is not None:
            await self._reject(call, error)
            return False
        try:
            body = orjson.loads(call.req.body or b"{}")
        except orjson.JSONDecodeError:
            body = None
        if not isinstance(body, dict):
            message = "We could not parse the JSON body of your request. The body must be a JSON object."
            await self._reject(call, MantleReject(400, None, message))
            return False
        set_body(self.state, call.rec, body)
        call.rec["model"] = body.get("model") if isinstance(body.get("model"), str) else None
        try:
            if self.route == "decisions":
                raise MantleInvalid(DECISIONS_REJECT)
            facts: ChatFacts = (
                check_chat(body)
                if self.route == "chat"
                else check_responses(body, openai_prefix=self.route == "openai_responses")
            )
        except MantleInvalid as exc:
            await self._reject(call, exc.reject)
            return False
        call.op = "chat-stream" if facts.stream else "chat"
        call.rec.update(op=call.op, nonce=find_nonce(facts.prompt))
        call.model, call.prompt, call.body, call.max_tokens = facts.model, facts.prompt, body, facts.max_tokens
        call.logprobs, call.top_logprobs, call.include_usage = facts.logprobs, facts.top_logprobs, facts.include_usage
        return True

    async def _reject(self, call: Call, err: WireError | MantleReject) -> None:
        call.rec["outcome"] = "rejected"
        await self.send_error(call, err)

    def _rid(self, call: Call) -> str:
        return call.request_id()

    # --- Wire --------------------------------------------------------------------------------------------------------
    async def send_error(self, call: Call, err: WireError | MantleReject) -> None:
        headers = [_JSON, ("x-request-id", self._rid(call))]
        if isinstance(err, MantleReject):
            status, raw = err.status, error_body(err.message, "invalid_request_error", err.code, err.param)
            name = err.code or "invalid_request_error"
        else:
            status, raw, name = err.status, error_body(err.message, err.mantle_type, err.mantle_code), err.name
            if err.retry_after_s is not None:
                headers.append(("retry-after", str(max(1, round(err.retry_after_s)))))
        call.rec.update(status=status, error=name, t_first_byte_ns=self.state.clock.now_ns())
        await call.resp.send(status, headers, raw)

    async def send_completion(self, call: Call, comp: Completion) -> None:
        assert call.res is not None
        fault = call.res.fault
        if comp.error is not None and not comp.error.startswith("malformed:"):
            message = None if fault is None else fault.message
            await self.send_error(call, exception_as_http(exception_type_of(comp.error), message))
            return
        reasoning_only = fault is not None and fault.kind == "reasoning_only"
        body = self._responses_body(call, comp) if self.route != "chat" else self._chat_body(call, comp, reasoning_only)
        raw = orjson.dumps(body)
        if comp.error == "malformed:truncated_json":
            raw = raw[: len(raw) // 2]
        elif comp.error is not None:
            raw = b'{"id": "chatcmpl-x", "choices": [<malformed>'
        call.rec.update(status=200, t_first_byte_ns=self.state.clock.now_ns())
        if comp.text:
            call.rec["t_first_text_sent_ns"] = call.rec["t_first_byte_ns"]
        await call.resp.send(200, [_JSON, ("x-request-id", self._rid(call))], raw)

    def _chat_body(self, call: Call, comp: Completion, reasoning_only: bool) -> dict[str, Any]:
        message: dict[str, Any] = {
            "role": "assistant",
            "content": None if comp.refused else comp.text,
            "refusal": REFUSAL_TEXT if comp.refused else None,
        }
        if reasoning_only:
            message["reasoning_content"] = reasoning_text(64)
        choice = {
            "index": 0,
            "message": message,
            "logprobs": chat_logprobs(comp, call.top_logprobs) if call.logprobs else None,
            "finish_reason": STOP_TO_FINISH[comp.stop],
        }
        return {
            "id": "chatcmpl-" + self._rid(call).replace("-", ""),
            "object": "chat.completion",
            "created": int(time.time()),
            "model": call.model,
            "choices": [choice],
            "usage": None if comp.usage is None else mantle_usage(comp.usage),
            "service_tier": "default",  # live chat JSON carries it (Wave E fidelity)
        }

    def _responses_body(self, call: Call, comp: Completion) -> dict[str, Any]:
        """A minimal Responses object (Luna on `/openai/v1/responses`)."""
        rid = self._rid(call).replace("-", "")
        part = (
            {"type": "refusal", "refusal": REFUSAL_TEXT}
            if comp.refused
            else {"type": "output_text", "text": comp.text, "annotations": [], "logprobs": []}
        )
        usage = comp.usage or {}
        inp, out = usage.get("input_tokens", 0), usage.get("output_tokens", 0)
        incomplete = comp.stop == "max_tokens"
        return {
            "id": f"resp_{rid}",
            "object": "response",
            "created_at": int(time.time()),
            "status": "incomplete" if incomplete else "completed",
            "incomplete_details": {"reason": "max_output_tokens"} if incomplete else None,
            "error": None,
            "model": call.model,
            "output": [
                {"type": "reasoning", "id": f"rs_{rid}", "summary": []},
                {"type": "message", "id": f"msg_{rid}", "role": "assistant", "status": "completed", "content": [part]},
            ],
            "usage": {
                "input_tokens": inp,
                "input_tokens_details": {"cached_tokens": 0, "cache_write_tokens": 0},
                "output_tokens": out,
                "output_tokens_details": {"reasoning_tokens": 0},
                "total_tokens": inp + out,
            },
        }

    async def send_stream(self, call: Call, plan: StreamPlan) -> None:
        """Pace the plan as `data: <chunk>` lines: a role chunk, `reasoning_content` and `content` deltas, the
        finish chunk, the usage chunk (only with include_usage), `[DONE]`; faults break the stream as specified."""
        assert call.res is not None
        res, rec, clock = call.res, call.rec, self.state.clock
        cfg = res.rule.stream
        pad, stamped = (cfg.pad, cfg.stamp) if cfg is not None else (0, False)
        pacer = Pacer(clock, call.resp, call.req.received_ns)
        cid, created = "chatcmpl-" + self._rid(call).replace("-", ""), int(time.time())

        def chunk(delta: dict[str, Any], finish: str | None = None, logprobs: Any = None) -> bytes:
            payload: dict[str, Any] = {
                "id": cid,
                "object": "chat.completion.chunk",
                "created": created,
                "model": call.model,
                "choices": [{"index": 0, "delta": delta, "logprobs": logprobs, "finish_reason": finish}],
            }
            if call.include_usage:
                payload["usage"] = None
            return b"data: " + orjson.dumps(pad_member(payload, res.draw, pad)) + b"\n\n"

        for ev in plan.events:
            await pacer.wait(ev.at_ms)
            if ev.kind == "start":
                await call.resp.start(200, [*_SSE, ("x-request-id", self._rid(call))])
                rec.update(status=200, t_first_byte_ns=clock.now_ns())
                pacer.add(chunk({"role": "assistant", "content": ""}))
            elif ev.kind == "reasoning":
                pacer.add(chunk({"reasoning_content": reasoning_text(ev.chars)}))
            elif ev.kind == "text":
                text = stamp(clock, ev.text) if stamped else ev.text
                if rec["t_first_text_sent_ns"] is None:
                    rec["t_first_text_sent_ns"] = clock.now_ns()
                lp = (
                    {"content": [{**_token(text, 0.0), "top_logprobs": []}], "refusal": None} if call.logprobs else None
                )
                pacer.add(chunk({"content": text}, logprobs=lp))
            elif ev.kind == "stop":
                pacer.add(chunk({}, finish=STOP_TO_FINISH[ev.stop or "end_turn"]))
            elif ev.kind == "usage":
                if call.include_usage:
                    usage = {"id": cid, "object": "chat.completion.chunk", "created": created, "model": call.model}
                    usage.update(choices=[], usage=mantle_usage(ev.usage or {}))
                    pacer.add(b"data: " + orjson.dumps(pad_member(usage, res.draw, pad)) + b"\n\n")
            else:
                pacer.add(self._broken(cid, ev.error or "", call, pad))
                await pacer.flush()
                await call.resp.end()
                return
        if plan.hang:
            await pacer.flush()
            rec["outcome"] = "hang"
            await call.resp.wait_closed()
            return
        pacer.add(b"data: [DONE]\n\n")
        await pacer.flush()
        await call.resp.end()

    def _broken(self, cid: str, error: str, call: Call, pad: int) -> bytes:
        """The fault tail of an SSE stream (nothing after it, no `[DONE]`); an error chunk is padded like the rest."""
        if error == "malformed:bad_sse_line":
            return b'{"id": "' + cid.encode() + b'", "object": "chat.completion.chunk"}\n\n'
        if error == "malformed:no_done":
            return b""
        if error.startswith("malformed:"):
            return b'data: {"id": "' + cid.encode() + b'", "object": "chat.completion.chunk", "choi\n\n'
        assert call.res is not None
        fault = call.res.fault
        err = exception_as_http(exception_type_of(error), None if fault is None else fault.message)
        call.rec["error"] = err.name
        payload = pad_member(error_payload(err.message, err.mantle_type, err.mantle_code), call.res.draw, pad)
        return b"data: " + orjson.dumps(payload) + b"\n\n"
