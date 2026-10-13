"""Real resources of the stack's t_acme/main developer, created once per session for the schemathesis tests: the ids
that path parameters are replaced with in the "real id" pass of every case (sessions, turns, decisions, files), and
fresh disposable ones for the destructive operations. Every response is contract-validated."""

from __future__ import annotations

import time
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import orjson
from tests.qa.contract.fuzzkit import AGHOO_VERSION, TABLE, VALIDATOR
from tests.unit.media_pipeline.media_fixtures import png

TERMINAL_FILE = {"ready", "partial", "rejected", "failed"}
REPORT_DIR = Path(__file__).resolve().parents[3] / "reports" / "tmp" / "qa-contract"


@dataclass
class Real:
    """A validated httpx client of one principal plus the ids created with it."""

    http: httpx.Client
    ids: dict[str, str]

    def call(self, method: str, path: str, *, json: Any = None, **kwargs: Any) -> httpx.Response:
        if json is not None:
            kwargs["content"] = orjson.dumps(json)
            kwargs["headers"] = {"content-type": "application/json", **kwargs.get("headers", {})}
        response = self.http.request(method, path, **kwargs)
        match = TABLE.match(method, path.split("?")[0])
        assert match is not None, path
        VALIDATOR.check_response(
            match.operation.operation_id, response.status_code, dict(response.headers), response.content, json
        )
        return response

    def session(self) -> str:
        created = self.call("POST", "/v1/sessions", json={"data_class": "internal"})
        assert created.status_code == 201, created.text
        return str(created.json()["id"])

    def file(self, session_id: str | None = None, *, wait: bool = True) -> str:
        data = {"purpose": "input", **({"session_id": session_id} if session_id else {})}
        files = {"file": ("qa.png", png(64, 48), "image/png")}
        uploaded = self.call("POST", "/v1/files", data=data, files=files)
        assert uploaded.status_code == 202, uploaded.text
        file_id = str(uploaded.json()["id"])
        deadline = time.monotonic() + 60
        while wait and self.call("GET", f"/v1/files/{file_id}").json()["status"] not in TERMINAL_FILE:
            assert time.monotonic() < deadline, f"{file_id} not terminal after 60 s"
            time.sleep(0.5)
        return file_id


def tag() -> str:
    return f"qa-{uuid.uuid4().hex[:10]}"


def make_real(state: Mapping[str, Any], role: str = "developer") -> Real:
    key = state["tenants"]["t_acme"]["projects"]["main"]["principals"][role]["key"]
    headers = {"authorization": f"Bearer {key}", "aghoo-version": AGHOO_VERSION}
    real = Real(httpx.Client(base_url=state["endpoints"]["api"][0], headers=headers, timeout=90.0), {})
    session_id = real.session()
    turn = real.call(
        "POST",
        "/v1/turns",
        json={
            "session_id": session_id,
            "stream": False,
            "input": [{"role": "user", "content": [{"type": "input_text", "text": f"What is the capital? ({tag()})"}]}],
        },
    )
    assert turn.status_code == 200, turn.text
    decision = real.call(
        "POST",
        "/v1/decisions",
        json={
            "profile": "lightweight-model-decide",
            "escalation": "off",
            "input": f"Call me back ({tag()})",
            "questions": [{"type": "predicate", "name": "callback", "instructions": "The user asks for a call back."}],
        },
    )
    assert decision.status_code == 200, decision.text
    real.ids.update(
        session_id=session_id,
        turn_id=str(turn.json()["turn_id"]),
        decision_id=str(decision.json()["id"]),
        file_id=real.file(session_id),
        kind="raster",
        profile_id="router-default",
    )
    return real


ID_FIELDS = {"file_id": "file_id", "session_id": "session_id", "retry_of": "turn_id"}


def realize(node: Any, ids: Mapping[str, str]) -> Any:
    """A copy of a generated body with every id field (`file_id`, `session_id`, `retry_of`) set to a real one."""
    if isinstance(node, dict):
        return {
            k: ids[ID_FIELDS[k]] if k in ID_FIELDS and isinstance(v, str) else realize(v, ids) for k, v in node.items()
        }
    if isinstance(node, list):
        return [realize(v, ids) for v in node]
    return node
