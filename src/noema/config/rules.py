"""Config rules: the vendored design checker plus the server-only Phase 1 rules (spec-05 §3).

`semantic_errors` is the vendored `check_config.semantic_errors` (design rules plus P1-C1-C4). `p1_errors` adds
P1-T1, P1-R1, P1-D1-D6, P1-G1, P1-L1 and P1-P1. Both take the schema-valid, default-filled config dict and return
one message per violation (empty = valid); every P1 message ends with its rule id.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, Final, Literal

from noema.contracts import vendor

NoemaEnv = Literal["test", "dev", "staging", "prod"]

P1_ELICITATIONS: Final = frozenset({"token_logprobs", "verbalized"})
P1_TEMPLATES: Final[Mapping[str, str]] = {  # implemented template -> its elicitation (P1-D5)
    "logprob-codes@2": "token_logprobs",
    "verbalized-dist@1": "verbalized",
    "verify-dist@1": "verbalized",
}
OUT_OF_PHASE_MODES: Final = frozenset({"best_of_n", "fuse"})
FILE_PRICES_ENVS: Final = frozenset({"dev", "test"})


def semantic_errors(cfg: Mapping[str, Any]) -> list[str]:
    """The vendored design rules (`check_config.semantic_errors`)."""
    return vendor.config_semantic_errors(cfg)


def flat_limit(cfg: Mapping[str, Any], profile: Mapping[str, Any]) -> int | None:
    """L(profile) = min(templates[m.template].max_choices) over all members; None if a template is unknown."""
    limits = [cfg["templates"].get(m["template"], {}).get("max_choices") for m in profile["members"]]
    return None if any(x is None for x in limits) else min(limits)


def option_count(question: Mapping[str, Any]) -> int | None:
    """K: predicate 2, choice len(choices), score len(levels); None for runtime-filled (dynamic) choices."""
    if question["type"] == "predicate":
        return 2
    if question["type"] == "score":
        return len(question["levels"])
    return None if "choices" not in question else len(question["choices"])


def _rule_t1(cfg: Mapping[str, Any]) -> list[str]:
    dp = cfg["decision_profiles"]
    return [
        f"tenants.{tid}.defaults.decision_profile: unknown decision profile '{pid}' (P1-T1)"
        for tid, t in cfg["tenants"].items()
        if (pid := t.get("defaults", {}).get("decision_profile")) not in dp
    ]


def _rules_d(cfg: Mapping[str, Any]) -> list[str]:
    errs: list[str] = []
    routing = cfg["routing"]
    if routing["speculation"].get("commit_on_quorum"):
        errs.append("routing.speculation.commit_on_quorum must be false in Phase 1 (P1-D1)")
    for pid, p in cfg["decision_profiles"].items():
        where = f"decision_profiles.{pid}"
        if p.get("batch_questions"):
            errs.append(f"{where}.batch_questions must not be true in Phase 1 (P1-D2)")
        if p["aggregation"] != "linear_pool":
            errs.append(f"{where}.aggregation must be linear_pool in Phase 1 (P1-D4)")
        for i, m in enumerate(p["members"]):
            if m["elicitation"] not in P1_ELICITATIONS:
                errs.append(f"{where}.members[{i}].elicitation '{m['elicitation']}' is not implemented (P1-D3)")
            expected = P1_TEMPLATES.get(m["template"])
            if expected is None:
                errs.append(f"{where}.members[{i}].template '{m['template']}' is not implemented (P1-D5)")
            elif expected != m["elicitation"]:
                errs.append(f"{where}.members[{i}].template '{m['template']}' needs elicitation {expected} (P1-D5)")
    errs += _rule_d6(cfg)
    return errs


def _rule_d6(cfg: Mapping[str, Any]) -> list[str]:
    routing, dp = cfg["routing"], cfg["decision_profiles"]
    asked: list[tuple[str, Sequence[str] | None]] = [(routing["router_profile"], None)]
    ver = routing.get("verification") or {}
    if "decision_profile" in ver:
        asked.append((ver["decision_profile"], ver.get("questions")))
    errs: list[str] = []
    for pid, select in asked:
        profile = dp.get(pid)
        if profile is None:
            continue  # reported by the design rules
        limit = flat_limit(cfg, profile)
        for q in profile.get("questions", []):
            if select is not None and q["name"] not in select:
                continue
            k = option_count(q)
            if limit is not None and k is not None and k > limit:
                errs.append(
                    f"decision_profiles.{pid}: platform question '{q['name']}' has {k} options, above the "
                    f"profile's flat limit {limit} (P1-D6)"
                )
    return errs


def _rule_g1(cfg: Mapping[str, Any]) -> list[str]:
    gp, routing = cfg["generation_profiles"], cfg["routing"]
    targets: list[tuple[str, str]] = [(f"routing.intent_routes.{k}", v) for k, v in routing["intent_routes"].items()]
    targets += [(f"routing.{k}", routing[k]) for k in ("default_profile", "uncertain_profile") if k in routing]
    for i, b in enumerate(routing["bindings"]):
        target = b["then"].get("generation_profile")
        if target and not target.startswith("intent_routes["):
            targets.append((f"routing.bindings[{i}].then.generation_profile", target))
    for tid, t in cfg["tenants"].items():
        if gid := t.get("defaults", {}).get("generation_profile"):
            targets.append((f"tenants.{tid}.defaults.generation_profile", gid))
    return [
        f"{where}: generation profile '{gid}' uses mode {gp[gid]['mode']}, not available in Phase 1 (P1-G1)"
        for where, gid in targets
        if gid in gp and gp[gid]["mode"] in OUT_OF_PHASE_MODES
    ]


def _rule_l1(cfg: Mapping[str, Any]) -> list[str]:
    errs: list[str] = []
    for tid, t in cfg["tenants"].items():
        allowed = t.get("allowed_models")
        for alias in t.get("limits", {}).get("model_quotas", {}):
            if alias not in cfg["models"]:
                errs.append(f"tenants.{tid}.limits.model_quotas: unknown model '{alias}' (P1-L1)")
            elif allowed is not None and alias not in allowed:
                errs.append(f"tenants.{tid}.limits.model_quotas: model '{alias}' is not in allowed_models (P1-L1)")
    return errs


def _rule_p1(cfg: Mapping[str, Any], env: NoemaEnv) -> list[str]:
    ref = cfg.get("platform", {}).get("cost", {}).get("prices_ref")
    if isinstance(ref, str) and ref.startswith("file:") and env not in FILE_PRICES_ENVS:
        return [f"platform.cost.prices_ref: file: catalogs are allowed only in dev and test, not {env} (P1-P1)"]
    return []


def p1_errors(cfg: Mapping[str, Any], *, env: NoemaEnv) -> list[str]:
    """The server-only Phase 1 rules (spec-05 §3). The `db:<n>` existence half of P1-P1 is checked when the
    loader resolves the catalog."""
    errs = _rule_t1(cfg)
    if not cfg["routing"].get("default_profile"):
        errs.append("routing.default_profile is required in Phase 1 (P1-R1)")
    errs += _rules_d(cfg)
    errs += _rule_g1(cfg)
    errs += _rule_l1(cfg)
    errs += _rule_p1(cfg, env)
    return errs
