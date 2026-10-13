"""Gate X1 (06 §11 "contract tests green", spec-10 X1 "OpenAPI <-> 07 drift"), `make docs-drift`: every JSON example
of the API user guide validates against the bundled OpenAPI, as a request or as a response of its operation.

Classification (an example no rule claims fails the test, so a new example needs a rule): a curl body is the request
of its path's operation; a ```json fence in §2 / §4.5 is the 200 response of the curl just above it, the one in
§5.1 a createTurn request; the openai ```python call is a facade request; inline spans are InputParts,
Degradations, `"input"` forms or single request fields (DecisionCreateParams in §4, TurnCreateParams in §5-§6).
Complete examples validate against the schema as is. Abbreviated ones (`…`) validate against a relaxed copy without
`required`, `minItems`, `minProperties`, `pattern`, `format` and lengths (what an abbreviation or a placeholder
like `"dec_…"` legitimately breaks); types, enums, `const`, oneOf branches and closed objects still apply, so a
renamed or unknown field, a wrong type or an unknown enum value fails. Request examples also pass the vendored
request invariants, and the Jev response the `jev_invariants` with its request.
"""

from __future__ import annotations

import copy
import os
from pathlib import Path
from typing import Any, Final

import jsonschema_rs
import pytest
from tests.qa.contract.guide_examples import Example, extract

from noema.api.middleware.routing import OperationTable
from noema.contracts import vendor
from noema.contracts.bundle import BUNDLE_URI, Bundle, default_bundle

GUIDE: Final = Path(os.environ.get("NOEMA_DOCS_GUIDE", "/codes/aghooai/docs/noema/07-api-user-guide.md"))
NON_JSON: Final = {  # pseudo-syntax spans of the guide, not JSON examples
    '{mode: "none"}',
    '{"error": {"type", "code", "message", "param", "request_id", "retryable", "details"}}',
}
RELAXED_DROP: Final = {"required", "minItems", "minProperties", "pattern", "format", "minLength", "maxLength"}
NAME_MAPS: Final = {"properties", "patternProperties", "$defs", "dependentSchemas"}
DATA_KEYS: Final = {"enum", "const", "default", "examples", "example"}
S = "/components/schemas/"
CHECKS = vendor.load("check_examples")
pytestmark = pytest.mark.skipif(
    not GUIDE.exists() and os.environ.get("NOEMA_DOCS_REQUIRE") != "1", reason=f"{GUIDE} not found"
)


def relax(node: Any, *, names: bool = False) -> Any:
    if isinstance(node, list):
        return [relax(v) for v in node]
    if not isinstance(node, dict):
        return node
    if names:
        return {k: relax(v) for k, v in node.items()}
    return {
        k: v if k in DATA_KEYS else relax(v, names=k in NAME_MAPS) for k, v in node.items() if k not in RELAXED_DROP
    }


BUNDLE: Final = default_bundle()
RELAXED: Final = Bundle(relax(copy.deepcopy(BUNDLE.doc)))
TABLE: Final = OperationTable(BUNDLE.operations())
OPS: Final = {op.operation_id: op for op in BUNDLE.operations()}


def request_ptr(operation_id: str) -> str:
    return OPS[operation_id].body_ptrs["application/json"]


def response_ptr(operation_id: str, status: str = "200") -> str:
    return OPS[operation_id].responses[status]["application/json"]


def classify(example: Example, last_request: tuple[str, Any] | None) -> tuple[str, str, Any]:
    """(what, schema pointer, value to validate) for one example; AssertionError for an example no rule claims."""
    s, v = example.section, example.value
    if example.source == "curl":
        match = TABLE.match("POST", example.path or "")
        assert match is not None, f"line {example.line}: no POST operation for {example.path}"
        return f"{match.operation.operation_id} request", request_ptr(match.operation.operation_id), v
    if example.source == "python":
        return "createDecisionOpenAICompat request", request_ptr("createDecisionOpenAICompat"), v
    if example.source == "json" and s.startswith(("2.", "4.5")) and last_request is not None:
        return f"{last_request[0]} 200 response", response_ptr(last_request[0]), v
    if example.source == "json" and s.startswith("5.1"):
        return "createTurn request", request_ptr("createTurn"), v
    if example.source == "inline" and isinstance(v, dict):
        if set(v) == {"input"}:
            return "decision input form", f"{S}DecisionCreateParams/properties/input", v["input"]
        if str(v.get("type", "")).startswith("input_"):
            return "InputPart", f"{S}InputPart", v
        if set(v) <= {"from", "to", "reason", "file_id", "detail"} and "reason" in v:
            return "Degradation", f"{S}Degradation", v
        (key,) = v
        owner = "DecisionCreateParams" if s.startswith("4") else "TurnCreateParams"
        assert key in BUNDLE.at(f"{S}{owner}/properties"), f"line {example.line}: {owner} has no field {key!r}"
        return f"{owner}.{key}", f"{S}{owner}/properties/{key}", v[key]
    raise AssertionError(f"line {example.line} ({example.source} in {s!r}): no rule classifies {example.text[:80]!r}")


def errors(ptr: str, value: Any, *, abbreviated: bool) -> list[str]:
    bundle = RELAXED if abbreviated else BUNDLE
    return [f"{list(e.instance_path)}: {e.message[:200]}" for e in bundle.validator(ptr).iter_errors(value)]


def examples() -> list[Example]:
    return sorted(extract(GUIDE), key=lambda e: e.line) if GUIDE.exists() else []


def test_guide_examples_are_found() -> None:
    found = examples()
    assert GUIDE.exists(), f"{GUIDE} is missing (NOEMA_DOCS_GUIDE)"
    assert sum(e.source in ("json", "curl", "python") for e in found) >= 9, [(e.line, e.source) for e in found]
    assert {e.text for e in found if e.value is None} == NON_JSON


def test_every_guide_example_validates_against_the_openapi() -> None:
    failures: list[str] = []
    last_request: tuple[str, Any] | None = None
    for example in examples():
        if example.value is None:
            continue
        what, ptr, value = classify(example, last_request)
        where = f"07 line {example.line} ({what}{', abbreviated' if example.abbreviated else ''})"
        failures += [f"{where}: {err}" for err in errors(ptr, value, abbreviated=example.abbreviated)]
        if what.endswith(" request") and example.source == "curl":
            last_request = (what.split()[0], example.value)
        if what == "createDecision request":
            failures += [f"07 line {example.line}: {e}" for e in CHECKS.request_invariants(example.value)]
        if what == "createDecisionOpenAICompat request":
            failures += [f"07 line {example.line}: {e}" for e in CHECKS.oa_request_invariants(example.value)]
        if what == "createDecision 200 response" and last_request and last_request[1].get("output_format") == "jev":
            failures += [f"07 line {example.line}: {e}" for e in CHECKS.jev_invariants(example.value, last_request[1])]
    assert not failures, "the guide drifted from the OpenAPI:\n" + "\n".join(failures)


_ = (BUNDLE_URI, jsonschema_rs)  # the relaxed bundle registers under the same URI as the committed one


def _rename(node: Any, old: str, new: str) -> Any:
    """A copy with the first key `old` (depth first) renamed to `new`."""
    done = False

    def walk(n: Any) -> Any:
        nonlocal done
        if isinstance(n, dict):
            out = {}
            for k, v in n.items():
                key = new if k == old and not done else k
                done = done or key != k
                out[key] = walk(v)
            return out
        return [walk(v) for v in n] if isinstance(n, list) else n

    return walk(node)


def _set_type(node: Any, value: str) -> Any:
    out = copy.deepcopy(node)
    first = next(iter(out["answers"].values())) if isinstance(out["answers"], dict) else out["answers"][0]
    first["type"] = value
    return out


MUTATIONS = [  # (example line marker, what to break): each must be caught
    ("curl /v1/decisions", lambda v: _rename(v, "choices", "options")),
    ("curl /v1/turns", lambda v: _rename(v, "session_id", "sessionId")),
    ("abbreviated native result", lambda v: _rename(v, "candidates", "candidate")),
    ("abbreviated native result", lambda v: _set_type(v, "multiple_choice")),
    ("jev response", lambda v: _rename(v, "probabilities", "probs")),
    ("turn request essentials", lambda v: {**v, "conditioning": "maybe"}),
]


def _pick(marker: str) -> Example:
    found = examples()
    by_marker = {
        "curl /v1/decisions": [e for e in found if e.source == "curl" and e.path == "/v1/decisions"],
        "curl /v1/turns": [e for e in found if e.source == "curl" and e.path == "/v1/turns"],
        "abbreviated native result": [e for e in found if e.source == "json" and e.value.get("object") == "decision"],
        "jev response": [e for e in found if e.source == "json" and not e.abbreviated and "decision_id" in e.value],
        "turn request essentials": [e for e in found if e.source == "json" and e.section.startswith("5.1")],
    }
    return by_marker[marker][0]


@pytest.mark.parametrize(("marker", "mutate"), MUTATIONS, ids=[f"{m}-{i}" for i, (m, _) in enumerate(MUTATIONS)])
def test_drift_check_catches_a_mutated_example(marker: str, mutate: Any) -> None:
    """The check has teeth: one renamed field, unknown enum value or answer type in a real example is reported."""
    example = _pick(marker)
    last: tuple[str, Any] | None = ("createDecision", {}) if example.source == "json" else None
    _, ptr, value = classify(example, last)
    assert not errors(ptr, value, abbreviated=example.abbreviated)
    assert errors(ptr, mutate(value), abbreviated=example.abbreviated), f"{marker}: mutation not caught"
