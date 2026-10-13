"""`POST /model/{modelId}/converse` (JSON) and `/converse-stream` (AWS eventstream), spec-10 §2.1a."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Final

import orjson
from tests.fakes.scenario import Completion, Op, StreamPlan

from .auth import parse_auth
from .common import pad_member, reasoning_text, signature, stamp
from .eventstream import CONTENT_TYPE, Corruption, event_frame, exception_frame, exception_type_of, raw_event_frame
from .faults import (
    VALIDATION_DEFAULT,
    WireError,
    bad_credential_scope,
    exception_as_http,
    exception_payload,
    signature_mismatch,
    unrecognized_client,
    validation,
)
from .httpserver import Request, Responder
from .models import INVALID_MODEL_ID, converse_model
from .pipeline import Call, Pacer, run
from .record import find_nonce, new_record, set_body
from .state import MockState
from .validate import Invalid, check_converse

_JSON: Final = ("content-type", "application/json")


def converse_usage(usage: Mapping[str, int], *, cache_fields: bool, max_tokens: int | None = None) -> dict[str, int]:
    """Converse `usage` from a scenario usage mapping (inputTokens excludes cache reads and writes); outputTokens is
    capped at the request's maxTokens, which Bedrock never exceeds (Wave E fidelity)."""
    inp, out = usage.get("input_tokens", 0), usage.get("output_tokens", 0)
    if max_tokens is not None:
        out = min(out, max_tokens)
    read, write = usage.get("cached_input_tokens", 0), usage.get("cache_write_input_tokens", 0)
    body = {"inputTokens": inp, "outputTokens": out, "totalTokens": inp + out + read + write}
    if cache_fields or read or write:
        body["cacheReadInputTokens"] = read
        body["cacheWriteInputTokens"] = write
    return body


class ConverseWire:
    """Validates and answers Converse/ConverseStream requests for one mock instance; SigV4 credentials must be
    scoped to `bedrock` in `region` (spec-10 §2.2)."""

    def __init__(self, state: MockState, *, region: str) -> None:
        self.state = state
        self.region = region

    async def handle(self, req: Request, resp: Responder, model_id: str, *, stream: bool) -> None:
        op: Op = "converse-stream" if stream else "converse"
        auth = parse_auth(req.headers)
        rec = new_record(self.state, req, op, model_id, auth)
        call = Call(req, resp, rec, op, model_id, "", {}, auth.akid)
        if not await self._prepare(call):
            rec["t_done_ns"] = self.state.clock.now_ns()
            return
        await run(self.state, call, self)

    async def _prepare(self, call: Call) -> bool:
        """Auth, JSON and the fidelity rules; False after sending the rejection."""
        auth = call.rec["auth"]
        if auth["scheme"] != "sigv4" or call.akid is None or not auth["session_token"]:
            await self._reject(call, unrecognized_client())
            return False
        scope_error = bad_credential_scope(auth["region"], auth["service"], self.region)
        if scope_error is not None:
            await self._reject(call, scope_error)
            return False
        if ":" in call.req.path:  # botocore signs the model id with ':' as %3A: re-quoted after signing
            await self._reject(call, signature_mismatch())
            return False
        try:
            body = orjson.loads(call.req.body or b"{}")
        except orjson.JSONDecodeError:
            body = None
        if not isinstance(body, dict):
            await self._reject(call, validation(VALIDATION_DEFAULT))
            return False
        set_body(self.state, call.rec, body)
        spec = converse_model(call.model)
        if spec is None:
            await self._reject(call, validation(INVALID_MODEL_ID))
            return False
        try:
            facts = check_converse(call.model, spec, body)
        except Invalid as exc:
            await self._reject(call, validation(exc.message))
            return False
        call.rec["nonce"] = find_nonce(facts.prompt)
        call.prompt, call.body, call.spec = facts.prompt, body, spec
        call.effort, call.max_tokens = facts.effort, facts.max_tokens
        return True

    async def _reject(self, call: Call, err: WireError) -> None:
        call.rec["outcome"] = "rejected"
        await self.send_error(call, err)

    def _rid(self, call: Call) -> str:
        return call.request_id()

    # --- Wire --------------------------------------------------------------------------------------------------------
    async def send_error(self, call: Call, err: WireError) -> None:
        headers = [_JSON, ("x-amzn-errortype", err.name), ("x-amzn-requestid", self._rid(call))]
        if err.retry_after_s is not None:
            headers.append(("retry-after", str(max(1, round(err.retry_after_s)))))
        call.rec.update(status=err.status, error=err.name, t_first_byte_ns=self.state.clock.now_ns())
        await call.resp.send(err.status, headers, orjson.dumps({"message": err.message}))

    async def send_completion(self, call: Call, comp: Completion) -> None:
        assert call.res is not None
        assert call.spec is not None
        spec = call.spec
        if comp.error is not None and not comp.error.startswith("malformed:"):
            fault = call.res.fault
            message = None if fault is None else fault.message
            await self.send_error(call, exception_as_http(exception_type_of(comp.error), message))
            return
        content: list[dict[str, Any]] = []
        fault = call.res.fault
        if comp.hidden_reasoning or (fault is not None and fault.kind == "reasoning_only"):
            content.append(self._reasoning_block(call))
        if comp.text:
            content.append({"text": comp.text})
        latency = round((self.state.clock.now_ns() - call.req.received_ns) / 1e6)
        body: dict[str, Any] = {
            "output": {"message": {"role": "assistant", "content": content}},
            "stopReason": comp.stop,
            "metrics": {"latencyMs": latency},
        }
        if comp.usage is not None:
            body["usage"] = converse_usage(comp.usage, cache_fields=spec.cache_usage, max_tokens=call.max_tokens)
        raw = orjson.dumps(body)
        if comp.error == "malformed:truncated_json":
            raw = raw[: len(raw) // 2]
        elif comp.error is not None:
            raw = b'{"output": {"message": <malformed>'
        call.rec.update(status=200, t_first_byte_ns=self.state.clock.now_ns())
        if comp.text:
            call.rec["t_first_text_sent_ns"] = call.rec["t_first_byte_ns"]
        await call.resp.send(200, [_JSON, ("x-amzn-requestid", self._rid(call))], raw)

    def _reasoning_block(self, call: Call) -> dict[str, Any]:
        """The reasoning block of a Converse JSON answer: Claude hides it (an empty text and a signature, live Haiku
        5.5), the OpenAI family redacts it, other families (GLM) show their reasoning text."""
        assert call.res is not None
        assert call.spec is not None
        if call.spec.signature:
            return {"reasoningContent": {"reasoningText": {"text": "", "signature": signature(call.res.draw)}}}
        if call.spec.family == "openai":
            return {"reasoningContent": {"redactedContent": signature(call.res.draw)}}
        return {"reasoningContent": {"reasoningText": {"text": reasoning_text(64)}}}

    async def send_stream(self, call: Call, plan: StreamPlan) -> None:
        """Pace the plan as eventstream frames: messageStart, reasoning deltas (block 0, closed with a signature for
        Claude; one `redactedContent` delta for the OpenAI family, which hides its reasoning as live Sol does), text
        deltas (next block), contentBlockStop, messageStop, metadata; errors end the stream."""
        assert call.res is not None
        assert call.spec is not None
        res, rec, clock = call.res, call.rec, self.state.clock
        cfg = res.rule.stream
        pad, stamped = (cfg.pad, cfg.stamp) if cfg is not None else (0, False)
        pacer = Pacer(clock, call.resp, call.req.received_ns)
        open_block: int | None = None
        next_block = 0

        def frame(event_type: str, payload: dict[str, Any]) -> bytes:
            return event_frame(event_type, pad_member(payload, res.draw, pad))

        def close_block() -> None:
            nonlocal open_block
            if open_block is None:
                return
            if open_block == 0 and next_block == 1 and call.spec is not None and call.spec.signature:
                sig = {"reasoningContent": {"signature": signature(res.draw)}}
                pacer.add(frame("contentBlockDelta", {"contentBlockIndex": 0, "delta": sig}))
            pacer.add(frame("contentBlockStop", {"contentBlockIndex": open_block}))
            open_block = None

        reasoning_block = False
        for ev in plan.events:
            await pacer.wait(ev.at_ms)
            if ev.kind == "start":
                await call.resp.start(200, [("content-type", CONTENT_TYPE), ("x-amzn-requestid", self._rid(call))])
                rec.update(status=200, t_first_byte_ns=clock.now_ns())
                pacer.add(frame("messageStart", {"role": "assistant"}))
            elif ev.kind == "reasoning":
                hidden = call.spec.family == "openai"
                if hidden and reasoning_block:
                    continue  # one redacted delta for the whole reasoning
                if not reasoning_block:
                    reasoning_block, open_block, next_block = True, 0, 1
                content = {"redactedContent": signature(res.draw)} if hidden else {"text": reasoning_text(ev.chars)}
                pacer.add(frame("contentBlockDelta", {"contentBlockIndex": 0, "delta": {"reasoningContent": content}}))
            elif ev.kind == "text":
                if open_block != next_block or open_block is None:
                    close_block()
                    open_block = next_block
                text = stamp(clock, ev.text) if stamped else ev.text
                if rec["t_first_text_sent_ns"] is None:
                    rec["t_first_text_sent_ns"] = clock.now_ns()
                pacer.add(frame("contentBlockDelta", {"contentBlockIndex": open_block, "delta": {"text": text}}))
            elif ev.kind == "stop":
                close_block()
                pacer.add(frame("messageStop", {"stopReason": ev.stop}))
            elif ev.kind == "usage":
                latency = round((clock.now_ns() - call.req.received_ns) / 1e6)
                usage = converse_usage(ev.usage or {}, cache_fields=False, max_tokens=call.max_tokens)
                pacer.add(frame("metadata", {"usage": usage, "metrics": {"latencyMs": latency}}))
            else:
                pacer.add(self._error_frame(call, ev.error or "modelStreamErrorException", pad))
                await pacer.flush()
                await call.resp.end()
                return
        await pacer.flush()
        if plan.hang:
            rec["outcome"] = "hang"
            await call.resp.wait_closed()
            return
        await call.resp.end()

    def _error_frame(self, call: Call, error: str, pad: int) -> bytes:
        """`exception_event` -> an exception frame (padded like every payload); `malformed`/`truncated_json` -> a
        broken frame."""
        if error.startswith("malformed:"):
            variant = error.split(":", 1)[1]
            if variant == "truncated_json":
                return raw_event_frame("contentBlockDelta", b'{"contentBlockIndex": 0, "delta": {"te')
            corrupt: Corruption = "message_crc" if variant in ("bad_crc", "malformed") else "prelude_crc"
            return event_frame("contentBlockDelta", {"contentBlockIndex": 0, "delta": {"text": "?"}}, corrupt=corrupt)
        fault = None if call.res is None else call.res.fault
        exception_type = exception_type_of(error)
        payload = exception_payload(exception_type, None if fault is None else fault.message)
        call.rec["error"] = exception_type
        return exception_frame(exception_type, pad_member(payload, None if call.res is None else call.res.draw, pad))
