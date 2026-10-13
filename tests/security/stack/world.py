"""Resources of the stack-level authz matrix (SEC-AZ-01): for each owner principal one session holding one turn, one
standalone decision and one ready file. Owners: the caller itself (`own`), `developer2` of the caller's project
(`peer`), the developer of `second` (`other_project`) and the developer of t_beta's `main` (`other_tenant`).
Callers are the five roles of t_acme's `main`."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Final

from tests.e2e.conftest import CheckedClient, Stack
from tests.e2e.suite.kit import Json, eventually, tag, turn_body
from tests.security.authz_oracle import Relation, Role
from tests.unit.media_pipeline import media_fixtures as fx

CALLER_TENANT: Final = "t_acme"
CALLER_PROJECT: Final = "main"
OWNERS: Final[dict[Relation, tuple[str, str, str]]] = {
    "peer": ("t_acme", "main", "developer2"),
    "other_project": ("t_acme", "second", "developer"),
    "other_tenant": ("t_beta", "main", "developer"),
}
QUESTION: Final[Json] = {"type": "predicate", "name": "callback", "instructions": "The user asks for a call back."}


@dataclass(frozen=True)
class Owned:
    """What one owner created."""

    session: str
    turn: str
    decision: str
    file: str


def owner_of(relation: Relation, caller: Role) -> tuple[str, str, str]:
    return (CALLER_TENANT, CALLER_PROJECT, caller) if relation == "own" else OWNERS[relation]


def decision_body(**extra: Any) -> Json:
    body: Json = {"profile": "router-default", "escalation": "off", "questions": [QUESTION]}
    return body | {"input": f"Call me ({tag()})"} | extra


def image_part(file_id: str) -> Json:
    return {"type": "input_image", "file_id": file_id}


def text_turn(*parts: Json, **extra: Any) -> Json:
    """A `stream: false` turn whose user message is a unique text plus `parts`."""
    body = turn_body(f"What is the capital of France? ({tag('az')})", stream=False, **extra)
    body["input"][0]["content"].extend(parts)
    return body


async def new_session(client: CheckedClient) -> str:
    return str((await client.request("POST", "/v1/sessions", json={}, expect=201)).json()["id"])


async def new_file(client: CheckedClient, *, ready: bool = True) -> str:
    files = {"file": ("authz.png", fx.png(24, 24), "image/png")}
    upload = await client.request("POST", "/v1/files", data={"purpose": "input"}, files=files, expect=202)
    file_id = str(upload.json()["id"])
    if ready:
        await eventually(client, f"/v1/files/{file_id}", lambda b: b["status"] == "ready", timeout_s=90)
    return file_id


async def create_owned(stack: Stack, tenant: str, project: str, role: str) -> Owned:
    client = stack.client(0, tenant=tenant, project=project, role=role)
    session = await new_session(client)
    turn = (await client.request("POST", "/v1/turns", json=text_turn(session_id=session), expect=200)).json()
    decision = (await client.request("POST", "/v1/decisions", json=decision_body(), expect=200)).json()
    return Owned(session, str(turn["turn_id"]), str(decision["id"]), await new_file(client))


class World:
    """The owned resources, created once per owner principal (lazily)."""

    def __init__(self, stack: Stack) -> None:
        self.stack = stack
        self._owned: dict[tuple[str, str, str], Owned] = {}

    async def owned(self, relation: Relation, caller: Role) -> Owned:
        owner = owner_of(relation, caller)
        if owner not in self._owned:
            self._owned[owner] = await create_owned(self.stack, *owner)
        return self._owned[owner]

    def known(self) -> dict[tuple[str, str, str], Owned]:
        return dict(self._owned)

    def caller(self, role: Role, replica: int = 1) -> CheckedClient:
        return self.stack.client(replica, tenant=CALLER_TENANT, project=CALLER_PROJECT, role=role)

    def owner_client(self, relation: Relation, caller: Role) -> CheckedClient:
        tenant, project, role = owner_of(relation, caller)
        return self.stack.client(0, tenant=tenant, project=project, role=role)
