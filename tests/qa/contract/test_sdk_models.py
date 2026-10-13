"""SDK compatibility, Python (spec-08 §10.4, 03 §9): the strict server models (`noema.contracts._generated.models`,
datamodel-codegen over the bundle) against the tolerant client models (vendored `python_sdk_types`, also the SDK's
`aghoo._decision_types`) and the bundle's JSON Schema, on every contract example (decision results, Jev responses,
decision and facade requests and responses). No stack needed.

Pass criteria: (1) the strict model accepts an example iff the bundle schema does (the generator kept the schema);
(2) every example the strict model accepts, the tolerant model accepts too, and its dump carries the example's
values unchanged (no coercion, no answer parsed as another type); (3) additive changes (an unknown field, an
unknown answer type) are rejected by the strict models and tolerated by the client models (`UnknownAnswer`).
"""

from __future__ import annotations

import copy
from datetime import datetime
from pathlib import Path
from typing import Any, Final

import orjson
import pytest
from pydantic import BaseModel, ValidationError

from noema.contracts import vendor
from noema.contracts._generated import models
from noema.contracts.bundle import VENDOR_DIR, default_bundle

pytestmark = pytest.mark.unit
EXAMPLES: Final = VENDOR_DIR / "examples"
SDK: Final = vendor.load("python_sdk_types")
KINDS: Final[dict[str, tuple[type[BaseModel], str, Any]]] = {  # prefix -> (strict model, schema, tolerant model)
    "decision-result": (models.DecisionResult, "DecisionResult", SDK.DecisionResult),
    "decision-jev-response": (models.JevDecisionResponse, "JevDecisionResponse", SDK.JevDecisionResponse),
    "decision-request": (models.DecisionCreateParams, "DecisionCreateParams", None),
    "openai-request": (models.OADecisionCreateParams, "OADecisionCreateParams", None),
    "openai-response": (models.OADecisionResponse, "OADecisionResponse", None),
}
KNOWN_LOOSE: Final = {"decision-result.invalid-calibrated-without-holdout.json"}  # the if/then "calibrated =>
# holdout" rule, which the generated models do not carry (spec-08 §0, IMPL-CON-10; the request gate runs first)
FILES: Final = sorted(p for p in EXAMPLES.glob("*.json") if p.name.split(".")[0] in KINDS)


def accepts(model: Any, value: Any) -> BaseModel | None:
    """The model parsed from the JSON text (strict models parse ISO date-times only in JSON mode, as the server
    and the SDKs do: they read wire bytes)."""
    try:
        parsed: BaseModel = model.model_validate_json(orjson.dumps(value))
        return parsed
    except ValidationError:
        return None


def carried(dumped: Any, original: Any, where: str = "$") -> list[str]:
    """Paths where the tolerant dump differs from the example (keys the tolerant model drops are ignored)."""
    if isinstance(dumped, dict) and isinstance(original, dict):
        return [p for k, v in dumped.items() if k in original for p in carried(v, original[k], f"{where}.{k}")]
    if isinstance(dumped, list) and isinstance(original, list):
        if len(dumped) != len(original):
            return [f"{where}: {len(dumped)} items != {len(original)}"]
        return [
            p for i, (d, o) in enumerate(zip(dumped, original, strict=True)) for p in carried(d, o, f"{where}[{i}]")
        ]
    numbers = all(isinstance(x, int | float) and not isinstance(x, bool) for x in (dumped, original))
    same = dumped == original and (numbers or type(dumped) is type(original))  # JSON 0 and 0.0 are one number
    return [] if same or _same_instant(dumped, original) else [f"{where}: {dumped!r} != {original!r}"]


def _same_instant(a: Any, b: Any) -> bool:
    """Two RFC 3339 date-times of one instant (`...00.000Z` dumps as `...00Z`)."""
    try:
        return isinstance(a, str) and isinstance(b, str) and datetime.fromisoformat(a) == datetime.fromisoformat(b)
    except ValueError:
        return False


def lossless(model: BaseModel, value: Any) -> list[str]:
    return carried(orjson.loads(model.model_dump_json(by_alias=True, exclude_unset=True)), value)


@pytest.mark.parametrize("path", FILES, ids=[p.name for p in FILES])
def test_strict_model_agrees_with_the_schema_and_the_tolerant_model(path: Path) -> None:
    strict, schema, tolerant = KINDS[path.name.split(".")[0]]
    value = orjson.loads(path.read_bytes())
    schema_ok = default_bundle().validator(f"/components/schemas/{schema}").is_valid(value)
    parsed = accepts(strict, value)
    loose = path.name in KNOWN_LOOSE
    assert (parsed is not None) == (schema_ok or loose), (
        f"strict model {'accepts' if parsed else 'rejects'}, schema {schema_ok}"
    )
    if ".valid" in path.name:
        assert parsed is not None, "a valid contract example is rejected by the strict server model"
        assert lossless(parsed, value) == []
    if tolerant is None or parsed is None:
        return
    client = accepts(tolerant, value)
    # the vendored self-test (python_sdk_types.py __main__): every invalid Jev example is rejected; a result only
    # for the model-level rules (MODEL_LEVEL_INVALID), the rest needs the request or the routing config
    invalid = path.stem.split(".invalid-")[1] if ".invalid-" in path.name else None
    expect_reject = invalid is not None and (path.name.startswith("decision-jev") or invalid in SDK.MODEL_LEVEL_INVALID)
    assert (client is None) == expect_reject, "tolerant model: accepts/rejects disagrees with the vendored self-test"
    if client is not None:
        assert lossless(client, value) == []


ADDITIVE = sorted(p for p in FILES if ".valid" in p.name and KINDS[p.name.split(".")[0]][2] is not None)


@pytest.mark.parametrize("path", ADDITIVE, ids=[p.name for p in ADDITIVE])
def test_additive_changes_break_the_strict_models_only(path: Path) -> None:
    strict, _, tolerant = KINDS[path.name.split(".")[0]]
    value = orjson.loads(path.read_bytes())
    widened = {**value, "added_in_a_later_version": {"x": 1}}
    assert accepts(strict, widened) is None and accepts(tolerant, widened) is not None
    if isinstance(value["answers"], list):  # native results: an answer type this SDK version does not know
        future = copy.deepcopy(value)
        future["answers"][0] = {"type": "ranking", "name": "future", "ranking": ["a", "b"]}
        assert accepts(strict, future) is None
        parsed = accepts(tolerant, future)
        assert parsed is not None
        first = getattr(parsed, "answers")[0]  # noqa: B009 (the tolerant model is loaded dynamically)
        assert (first.type, first.raw_type, first.name) == ("unknown", "ranking", "future")
