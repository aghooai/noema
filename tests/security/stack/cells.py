"""SEC-AZ-01 cells GENERATED from the bundled OpenAPI: every authenticated operation x the five roles x the resource
relations (own / peer / other project / other tenant) of each resource it names (a path parameter, or a body
field: createTurn/createDecision `session_id` and file parts, uploadFile `session_id`). An operation the bundle adds
without a request builder here fails `test_every_operation_has_cells`."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Final

from tests.e2e.suite.kit import Json, tag
from tests.security.authz_oracle import RELATIONS, ROLES, Kind, Relation, Role
from tests.security.stack.world import Owned, decision_body, image_part, text_turn
from tests.unit.media_pipeline import media_fixtures as fx

from noema.contracts.bundle import default_bundle

PATH_KINDS: Final[dict[str, Kind]] = {
    "turn_id": "turn",
    "decision_id": "decision",
    "session_id": "session",
    "file_id": "file",
}
# Body-named resources: operationId -> {variant: kind} (variant "" = the request names no resource).
BODY_VARIANTS: Final[dict[str, dict[str, Kind | None]]] = {
    "createTurn": {"": None, "session_id": "session", "file": "file"},
    "createDecision": {"": None, "session_id": "session", "file": "file"},
    "uploadFile": {"": None, "session_id": "session"},
}
DESTRUCTIVE: Final = frozenset({"deleteSession", "deleteFile"})
NO_RESOURCE: Final = frozenset(
    {"createSession", "listSessions", "listProfiles", "getUsage", "createDecisionOpenAICompat", "listCalibrators",
     "listLabelingRuns", "listLabelingDropped"}
)  # fmt: skip
QUERY: Final[dict[str, dict[str, str]]] = {"getUsage": {"group_by": "project"}}
NO_RELATION: Final[tuple[Relation, ...]] = ("none",)


@dataclass(frozen=True)
class Cell:
    operation: str
    method: str
    path_template: str
    role: Role
    relation: Relation
    kind: Kind | None
    variant: str = ""
    params: dict[str, str] = field(default_factory=dict)

    @property
    def name(self) -> str:
        what = f"{self.kind}@{self.relation}" if self.kind else "-"
        return f"{self.operation}[{self.variant or 'path' if self.kind else ''}] {self.role} {what}"


def facade_body() -> Json:
    text = f"Export fails in Safari but works in Chrome. ({tag('az')})"
    return {
        "model": "aghoo/router-default",
        "input": [{"role": "user", "type": "message", "content": text}],
        "questions": [{"type": "choice", "name": "regression", "instructions": "Is this a regression?",
                       "choices": [{"value": True}, {"value": False}]}],
    }  # fmt: skip


def path_of(cell: Cell, res: Owned | None) -> str:
    path = cell.path_template
    if res is not None:
        ids = {"turn_id": res.turn, "decision_id": res.decision, "session_id": res.session, "file_id": res.file}
        for name, value in ids.items():
            path = path.replace("{" + name + "}", value)
    return path.replace("{kind}", "raster")


def request_kwargs(cell: Cell, res: Owned | None) -> dict[str, Any]:
    """The httpx keyword arguments of the cell's request (JSON bodies through `json=`)."""
    op, v = cell.operation, cell.variant
    kw: dict[str, Any] = {"params": dict(QUERY.get(op, {}))}
    if op == "createTurn":
        assert res is not None or v == ""
        extra = {"session_id": res.session} if v == "session_id" and res else {}
        kw["json"] = text_turn(*([image_part(res.file)] if v == "file" and res else []), **extra)
    elif op == "createDecision":
        if v == "session_id" and res:
            kw["json"] = decision_body(session_id=res.session)
        elif v == "file" and res:
            content = [{"type": "input_text", "text": f"Call me ({tag()})"}, image_part(res.file)]
            kw["json"] = decision_body(input=[{"role": "user", "content": content}])
        else:
            kw["json"] = decision_body()
    elif op == "uploadFile":
        form = {"purpose": "input"} | ({"session_id": res.session} if v == "session_id" and res else {})
        kw.update(data=form, files={"file": ("authz.png", fx.png(16, 16), "image/png")})
    elif op == "createSession":
        kw["json"] = {}
    elif op == "updateSession":
        kw["json"] = {"metadata": {"authz": cell.role}}
    elif op == "createDecisionOpenAICompat":
        kw["json"] = facade_body()
    return kw


def generate() -> list[Cell]:
    """Every cell, destructive operations last (each allowed destructive cell gets a fresh resource)."""
    cells: list[Cell] = []
    for op in default_bundle().operations():
        if not op.authenticated:
            continue
        names = [p.name for p in op.parameters if p.location == "path" and p.name in PATH_KINDS]
        variants: dict[str, Kind | None]
        if names:
            variants = {"": PATH_KINDS[names[0]]}
        elif op.operation_id in BODY_VARIANTS:
            variants = BODY_VARIANTS[op.operation_id]
        elif op.operation_id in NO_RESOURCE:
            variants = {"": None}
        else:
            continue  # no builder: caught by test_every_operation_has_cells
        for variant, kind in variants.items():
            for role in ROLES:
                for relation in RELATIONS if kind else NO_RELATION:
                    if relation == "own" and role in ("reviewer", "auditor"):
                        continue  # they create nothing
                    cells.append(Cell(op.operation_id, op.method, op.path, role, relation, kind, variant))
    return sorted(cells, key=lambda c: c.operation in DESTRUCTIVE)
