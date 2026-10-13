"""SDK compatibility, TypeScript (spec-08 §10.3, IMPL-035): a fresh openapi-typescript 7.13.0 run over the bundle
(the root node_modules, `--default-non-nullable false`, as the SDK's sync script) equals the SDK's committed wire
types, and its `components["schemas"]` are assignable to the vendored public contract types (types.ts, sdk.ts,
openai-compat.ts) for every schema both declare: responses wire -> declared, request bodies declared -> wire
(deep-mutable), enums both ways. The one documented exception (sdk/typescript/test/wire-compat.check.ts):
openapi-typescript drops the allOf refinements of the answer union, so types that contain answers are compared
without `answers` (the client models check those at run time). A planted wrong assertion must fail (the harness
has teeth). No stack needed; runs node, no network.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path
from typing import Final

import pytest

from noema.contracts.bundle import BUNDLE_PATH, VENDOR_DIR

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
REPO: Final = Path(__file__).resolve().parents[3]
BIN: Final = REPO / "node_modules" / ".bin"
SDK_WIRE: Final = REPO / "sdk" / "typescript" / "src" / "generated" / "openapi.d.ts"
_T_RESPONSES: Final = [
    "HoldoutMetrics",
    "CalibrationBinding",
    "Calibration",
    "HeuristicConfidence",
    "MemberVote",
    "Usage",
    "QuorumInfo",
    "EscalationStage",
    "Provenance",
    "PredicateAnswer",
    "ChoiceAnswer",
    "ScoreAnswer",
    "RefusalAnswer",
    "UnavailableAnswer",
    "Answer",
    "DecisionResult",
    "JevAnswer",
    "JevDecisionResponse",
    "RoutingDecision",
    "Degradation",
    "TurnEvent",
    "TurnTimings",
]
_S_RESPONSES: Final = [
    "TurnObject",
    "Session",
    "SessionTurn",
    "FileObject",
    "ProfileInfo",
    "UsageLine",
    "UsageRow",
    "CalibratorInfo",
    "LabelingRun",
    "LabelingDroppedItem",
]
RESPONSES: Final = {  # wire schema -> the declared type it must be assignable to
    **{n: f"T.{n}" for n in _T_RESPONSES},
    **{n: f"S.{n}" for n in _S_RESPONSES},
    "Candidate": "T.Candidate<string | boolean>",
    "OAAnswer": "O.OAAnswer",
    "OADecisionResponse": "O.OADecisionResponse",
    "Error": "T.ErrorEnvelope",
}
_T_REQUESTS: Final = [
    "PredicateQuestion",
    "ChoiceQuestion",
    "ScoreQuestion",
    "Question",
    "InputPart",
    "DecisionCreateParams",
]
REQUESTS: Final = {  # the declared request type -> the wire schema it must be assignable to
    **{n: f"T.{n}" for n in _T_REQUESTS},
    "TurnCreateParams": "S.TurnCreateParams",
    "SessionUpdate": "S.SessionUpdate",
    **{n: f"O.{n}" for n in ["OAInputPart", "OAInputMessage", "OAQuestion", "OADecisionCreateParams"]},
}
ENUMS: Final = {
    "ErrorCode": "T.ErrorCode",
    "Effort": "T.Effort",
    "AnswerMode": "T.AnswerMode",
    "Strategy": "T.Strategy",
    "DerivativeKind": "S.DerivativeKind",
    "CalibratorStatus": "S.CalibratorStatus",
}
WITH_ANSWERS: Final = {
    "PredicateAnswer",
    "ChoiceAnswer",
    "ScoreAnswer",
    "Answer",
    "DecisionResult",
    "TurnEvent",
    "TurnObject",
}
HEADER: Final = """import type { components } from "./openapi";
import type * as T from "./types";
import type * as S from "./sdk";
import type * as O from "./openai-compat";
type W = components["schemas"];
type Assert<X extends Y, Y> = X;
type NoAnswers<X> = X extends readonly (infer E)[]
  ? NoAnswers<E>[]
  : X extends { answers: unknown }
    ? { [K in Exclude<keyof X, "answers">]: NoAnswers<X[K]> }
    : X extends { candidates: unknown; type: unknown }
      ? { [K in Exclude<keyof X, "candidates" | "choice" | "level">]: NoAnswers<X[K]> }
      : X extends object
        ? { [K in keyof X]: NoAnswers<X[K]> }
        : X;
type DeepMutable<X> = X extends readonly (infer E)[]
  ? DeepMutable<E>[]
  : X extends object
    ? { -readonly [K in keyof X]: DeepMutable<X[K]> }
    : X;
"""


def assertions() -> list[tuple[str, str]]:
    out = [(f"{w} wire -> {t}", f'Assert<W["{w}"], {t}>') for w, t in RESPONSES.items()]
    out += [(f"{w} {t} -> wire", f'Assert<DeepMutable<{t}>, W["{w}"]>') for w, t in REQUESTS.items()]
    out += [(f"{w} enum both ways", f'Assert<W["{w}"], {t}> | Assert<{t}, W["{w}"]>') for w, t in ENUMS.items()]
    out += [
        (f"{w} wire -> {RESPONSES[w]} without answers", f'Assert<NoAnswers<W["{w}"]>, NoAnswers<{RESPONSES[w]}>>')
        for w in sorted(WITH_ANSWERS)
    ]
    return out


def compile_checks(tmp: Path, extra: tuple[tuple[str, str], ...] = ()) -> tuple[list[tuple[str, str]], set[str]]:
    """Generates the wire types into `tmp`, compiles the assertions; returns (assertions, names that failed)."""
    subprocess.run(
        [
            str(BIN / "openapi-typescript"),
            str(BUNDLE_PATH),
            "--default-non-nullable",
            "false",
            "-o",
            str(tmp / "openapi.d.ts"),
        ],
        check=True,
        capture_output=True,
        cwd=REPO,
    )
    for name in ("types.ts", "sdk.ts", "openai-compat.ts"):
        shutil.copy(VENDOR_DIR / name, tmp / name)
    checks = [*assertions(), *extra]
    lines = [f"export type _{i} = {expr}; // {name}" for i, (name, expr) in enumerate(checks)]
    (tmp / "check.ts").write_text(HEADER + "\n".join(lines) + "\n")
    done = subprocess.run(
        [
            str(BIN / "tsc"),
            "--noEmit",
            "--strict",
            "--exactOptionalPropertyTypes",
            "--noUncheckedIndexedAccess",
            "--skipLibCheck",
            "--target",
            "ES2022",
            "--module",
            "ESNext",
            "--moduleResolution",
            "Bundler",
            str(tmp / "check.ts"),
        ],
        capture_output=True,
        text=True,
        cwd=REPO,
    )
    first = HEADER.count("\n") + 1
    failed_lines = {int(m.group(1)) for m in re.finditer(r"check\.ts\((\d+),\d+\): error", done.stdout)}
    other = [ln for ln in done.stdout.splitlines() if "error" in ln and "check.ts(" not in ln]
    assert not other, "errors outside the assertions:\n" + "\n".join(other[:10])
    return checks, {checks[n - first][0] for n in failed_lines}


def test_fresh_openapi_typescript_output_equals_the_sdk_wire_types(tmp_path: Path) -> None:
    compile_checks(tmp_path)
    fresh = (tmp_path / "openapi.d.ts").read_text().split("export interface paths", 1)[1]
    assert SDK_WIRE.read_text().split("export interface paths", 1)[1] == fresh


def test_wire_types_are_assignable_to_the_contract_types(tmp_path: Path) -> None:
    wrong = (("planted wrong assertion", 'Assert<W["Session"], { id: number }>'),)
    checks, failed = compile_checks(tmp_path, wrong)
    assert "planted wrong assertion" in failed, "tsc reported nothing for a wrong assertion"
    failed.discard("planted wrong assertion")
    documented = {f"{w} wire -> {RESPONSES[w]}" for w in WITH_ANSWERS}  # answers: see the module docstring
    print(f"{len(checks) - 1} assertions, failed: {sorted(failed)}")
    assert failed <= documented, f"not assignable: {sorted(failed - documented)}"
