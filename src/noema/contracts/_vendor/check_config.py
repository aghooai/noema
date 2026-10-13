"""Validate platform configuration: JSON Schema + cross-reference rules JSON Schema can't express.

Usage: python3 -I check_config.py [config.yaml ...]   (default: examples/platform.example.yaml)
Also runs negative tests (mutations of the example that must be rejected). Exit 0 iff all expectations hold.
"""
import copy
import json
import pathlib
import re
import sys

import yaml
from jsonschema import Draft202012Validator

HERE = pathlib.Path(__file__).parent
SCHEMA = json.loads((HERE / "config.schema.json").read_text())
Draft202012Validator.check_schema(SCHEMA)
VALIDATOR = Draft202012Validator(SCHEMA)

THRESHOLD_KEYS = ("p_max_gte", "probability_gte", "margin_gte", "confidence_band")
TIGHTENING = {  # actions that only make behavior safer (08 §3.1); exempt from the evidence rule
    "disable_tools": lambda v: v is True,
    "spotlight": lambda v: v == "strict",
    "response_cache": lambda v: v == "off",
    "log_content": lambda v: v in ("redacted", "none"),
    "verify_answer": lambda v: v is True,
}
LOGPROB_RESERVE = 4  # top_logprobs slots kept for EOS and whitespace variants of the option codes
INTENT_LOOKUP = re.compile(r"^intent_routes\[([a-z][a-z0-9_]{0,63})\]$")


def tightening_only(then):
    return all(k in TIGHTENING and TIGHTENING[k](v) for k, v in then.items())


def reachable_generation_profiles(cfg, rule=None):
    """Generation profiles a binding's effort / budget can land on (a pinned client profile is out of scope)."""
    routing, gp = cfg["routing"], cfg["generation_profiles"]
    target = (rule or {}).get("then", {}).get("generation_profile")
    if target and INTENT_LOOKUP.match(target):
        return sorted(set(routing["intent_routes"].values()) & set(gp))
    if target:
        return [target] if target in gp else []
    reach = {routing.get("default_profile"), routing["uncertain_profile"], *routing["intent_routes"].values()}
    reach |= {r["then"]["generation_profile"] for r in routing["bindings"]
              if r["then"].get("generation_profile") and not INTENT_LOOKUP.match(r["then"]["generation_profile"])}
    return sorted(g for g in reach if g in gp)


def early_commit_grammar_errors(cfg, router):
    """Restricted grammar for the [X] early-commit rule (02 §6.3): argmax certificate + all-vertex equal spec is sound only
    when the direct and low regions are convex in the missing members' mass, i.e. thresholds on p_max, margin and
    normalized_max over an uncalibrated linear pool. Anything else must wait or use bounded holdback."""
    errs = []
    if router["aggregation"] != "linear_pool":
        errs.append("commit_on_quorum needs aggregation linear_pool on the router profile")
    router_q = {q["name"]: q for q in router.get("questions", [])}
    bands = cfg["routing"]["confidence_bands"]
    for i, rule in enumerate(cfg["routing"]["bindings"]):
        w = rule["when"]
        if w["answer"] not in router.get("commit_critical", []) or w.get("source", "platform") != "platform":
            continue
        where = f"routing.bindings[{i}]"
        q = router_q.get(w["answer"])
        if q is None:  # reported by the commit_critical rule
            continue
        if "p_max_gte" in w and q["type"] == "choice":
            k = len(q["choices"])
            low_cut = 1 / k + bands["low_lt"] * (1 - 1 / k)  # p_max below this is the low band (normalized_max < low_lt)
            if w["p_max_gte"] < low_cut:
                errs.append(f"{where}: the direct region (p_max >= {w['p_max_gte']}) overlaps the low band (p_max < {low_cut:.3f}); the certified regions must be disjoint")
        if w.get("requires_calibrated") is True:
            errs.append(f"{where}: commit_on_quorum cannot certify a calibrated binding on a commit-critical answer")
        if w.get("mode") == "expected_utility":
            errs.append(f"{where}: commit_on_quorum cannot certify expected_utility on a commit-critical answer")
        if "probability_gte" in w:
            errs.append(f"{where}: probability_gte is outside the certified grammar (p_max, margin, normalized_max only)")
        if "confidence_band" in w:
            if q["type"] == "score":
                errs.append(f"{where}: ordinal_dispersion bands are not convex in the missing mass; use level_in on a score answer")
            if w["confidence_band"] == "medium":
                errs.append(f"{where}: the medium band is not convex; only direct (high) and low regions are certified")
    return errs


def semantic_errors(cfg):
    errs, models = [], cfg["models"]
    dp, gp, routing = cfg["decision_profiles"], cfg["generation_profiles"], cfg["routing"]

    def need_model(alias, role, where):
        m = models.get(alias)
        if m is None:
            errs.append(f"{where}: unknown model '{alias}'")
        elif role not in m["roles"]:
            errs.append(f"{where}: model '{alias}' lacks role '{role}'")
        return m

    def check_effort(m, effort, where):
        if m is not None and effort is not None and effort not in m["controls"]["effort"]:
            errs.append(f"{where}: effort '{effort}' not supported by {m['id']} (supports {m['controls']['effort']})")

    templates = cfg["templates"]
    for tid, tpl in templates.items():
        cap = {"token_logprobs": 16, "verbalized": 26}.get(tpl["elicitation"])
        if cap and tpl["max_choices"] > cap:
            errs.append(f"templates.{tid}: max_choices {tpl['max_choices']} above the {tpl['elicitation']} limit {cap}")
        if tpl["elicitation"] == "token_logprobs":
            for alias, m in models.items():
                top = m["controls"].get("top_logprobs_max")
                if m["controls"]["logprobs"] and top and tpl["max_choices"] > top - LOGPROB_RESERVE:
                    errs.append(f"templates.{tid}: max_choices {tpl['max_choices']} above top_logprobs_max {top} of {m['id']} minus the reserve {LOGPROB_RESERVE}")

    for pid, p in dp.items():
        required = [mem for mem in p["members"] if not mem.get("optional")]
        if sum(mem["weight"] for mem in required) <= 0:
            errs.append(f"decision_profiles.{pid}: required member weights sum to 0 (linear pool undefined)")
        seen = set()
        for i, mem in enumerate(p["members"]):
            ident = (mem["model"], mem["elicitation"], mem["template"], mem.get("effort"))
            if ident in seen:
                errs.append(f"decision_profiles.{pid}.members[{i}]: duplicate member {ident}; repeated votes of one model are not independent (use `samples` inside one member)")
            seen.add(ident)
        distinct = {mem["model"] for mem in required if mem["weight"] > 0}  # a model counts once toward quorum
        if p["min_quorum"] > len(distinct):
            errs.append(f"decision_profiles.{pid}: min_quorum {p['min_quorum']} > {len(distinct)} distinct required models")
        for i, mem in enumerate(p["members"]):
            where = f"decision_profiles.{pid}.members[{i}]"
            if "backup_after_ms" in mem and mem["backup_after_ms"] + mem["latency_p95_ms"] > p["deadline_ms"]:
                errs.append(f"{where}: backup_after_ms {mem['backup_after_ms']} + latency_p95_ms {mem['latency_p95_ms']} exceeds the {p['deadline_ms']} ms deadline; the backup could not land in time")
            tpl = templates.get(mem["template"])
            if tpl is None:
                errs.append(f"{where}: unknown template '{mem['template']}'")
            elif tpl["elicitation"] != mem["elicitation"]:
                errs.append(f"{where}: template {mem['template']} is {tpl['elicitation']}, member elicitation is {mem['elicitation']}")
            m = need_model(mem["model"], "decision" if pid != routing.get("verification", {}).get("decision_profile") and not _is_selector(cfg, pid) else "selector", where)
            check_effort(m, mem.get("effort"), where)
            if m and mem["elicitation"] == "token_logprobs" and not m["controls"]["logprobs"]:
                errs.append(f"{where}: token_logprobs needs a logprobs-capable model ({m['id']} has none)")
            if m and mem["elicitation"] == "sampling" and not m["controls"]["temperature"]:
                errs.append(f"{where}: sampling needs temperature control; {m['id']} rejects temperature (identical outputs)")
        qnames = [q["name"] for q in p.get("questions", [])]
        if len(qnames) != len(set(qnames)):
            errs.append(f"decision_profiles.{pid}: duplicate question names")
        for name in p.get("commit_critical", []):
            if name not in qnames:
                errs.append(f"decision_profiles.{pid}.commit_critical: '{name}' is not a question of this profile")
        limit = min((templates[mem["template"]]["max_choices"] for mem in required if mem["template"] in templates), default=255)
        for q in p.get("questions", []):  # the hierarchical method needs a second round trip: commit-critical questions stay flat
            if q["name"] in p.get("commit_critical", []) and len(q.get("choices", [])) > limit:
                errs.append(f"decision_profiles.{pid}.questions.{q['name']}: commit-critical question has {len(q['choices'])} choices, above the smallest member max_choices {limit}")
        esc = p.get("escalation")
        if esc and esc["to_profile"] not in dp:
            errs.append(f"decision_profiles.{pid}.escalation: unknown profile '{esc['to_profile']}'")
        for name in (esc or {}).get("questions", []):  # P1-C3 (IMPL-068): names platform questions of this profile
            if name not in qnames:
                errs.append(f"decision_profiles.{pid}.escalation.questions: '{name}' is not a question of this profile")
    # escalation cycles
    for start in dp:
        seen, cur = {start}, dp[start].get("escalation", {}).get("to_profile")
        while cur in dp:
            if cur in seen:
                errs.append(f"decision_profiles.{start}: escalation cycle via '{cur}'")
                break
            seen.add(cur)
            cur = dp[cur].get("escalation", {}).get("to_profile")

    for gid, g in gp.items():
        for i, mem in enumerate(g["members"] + ([g["synthesizer"]] if "synthesizer" in g else [])):
            where = f"generation_profiles.{gid}.members[{i}]"
            m = need_model(mem["model"], "generation", where)
            check_effort(m, mem.get("effort"), where)
            cap = (m or {}).get("max_output_tokens_cap")
            if cap and mem.get("max_output_tokens", 0) > cap:
                errs.append(f"{where}: max_output_tokens above model cap {cap}")
        if "selector" in g and g["selector"]["decision_profile"] not in dp:
            errs.append(f"generation_profiles.{gid}.selector: unknown decision profile")

    for name in ("router_profile",):
        if routing[name] not in dp:
            errs.append(f"routing.{name}: unknown decision profile '{routing[name]}'")
    for name in ("uncertain_profile", "default_profile"):
        if name in routing and routing[name] not in gp:
            errs.append(f"routing.{name}: unknown generation profile '{routing[name]}'")
    router_q = {q["name"]: q for q in dp.get(routing["router_profile"], {}).get("questions", [])}
    intent_values = {c["value"] for c in router_q.get("intent", {}).get("choices", [])}
    for intent, prof in routing["intent_routes"].items():
        if prof not in gp:
            errs.append(f"routing.intent_routes.{intent}: unknown generation profile '{prof}'")
        if intent_values and intent not in intent_values:
            errs.append(f"routing.intent_routes.{intent}: not a choice of the router's intent question")
    b = routing["confidence_bands"]
    if not b["low_lt"] < b["high_gte"]:  # P1-C2 (IMPL-039)
        errs.append("routing.confidence_bands: low_lt must be < high_gte")
    router_qs = dp.get(routing["router_profile"], {}).get("questions", [])
    router_q = {q["name"]: q for q in router_qs}
    reserved = set(router_q)
    for i, rule in enumerate(routing["bindings"]):
        where, w, then = f"routing.bindings[{i}]", rule["when"], rule["then"]
        target = then.get("generation_profile")
        if target and not INTENT_LOOKUP.match(target) and target not in gp:
            errs.append(f"{where}: unknown generation profile '{target}'")
        if target and target.startswith("intent_routes"):
            m = INTENT_LOOKUP.match(target)
            if not m:
                errs.append(f"{where}: malformed route expression '{target}' (expected intent_routes[<answer>])")
            elif m.group(1) != w["answer"] or w["answer"] != "intent":
                errs.append(f"{where}: '{target}' must reference the router's `intent` answer that this binding matches")
        source = w.get("source", "platform")
        if source == "request":
            if w["answer"] in reserved:
                errs.append(f"{where}: request-source binding uses reserved router name '{w['answer']}'")
            if "answer_type" not in w:
                errs.append(f"{where}: request-source binding must declare answer_type")
        else:
            q = router_q.get(w["answer"])
            if q is None:
                errs.append(f"{where}: binding refers to undefined platform question '{w['answer']}'")
            else:
                declared = [c["value"] for c in q.get("choices", [])] or ([True, False] if q["type"] == "predicate" else [])
                levels = [lv["label"] for lv in q.get("levels", [])]
                if "level_in" in w:
                    if q["type"] != "score":
                        errs.append(f"{where}: level_in needs a score question; '{w['answer']}' is {q['type']}")
                    for lv in w["level_in"]:
                        if lv not in levels:
                            errs.append(f"{where}: level '{lv}' is not a level of '{w['answer']}' (declared: {levels})")
                if "value_in" in w:
                    if q["type"] == "score":
                        errs.append(f"{where}: value_in needs a choice or predicate question; '{w['answer']}' is score")
                    for v in w["value_in"]:
                        if v not in declared:
                            errs.append(f"{where}: value {v!r} is not a value of '{w['answer']}' (declared: {declared})")
                if "probability_gte" in w and q["type"] != "predicate":
                    errs.append(f"{where}: probability_gte needs a predicate question; '{w['answer']}' is {q['type']}")
        # evidence rule: a threshold on a non-tightening binding is `rule` mode unless declared otherwise
        mode = w.get("mode") or ("rule" if any(k in w for k in THRESHOLD_KEYS) and not tightening_only(then) else None)
        if mode == "rule" and not (w.get("eval_ref") or w.get("requires_calibrated") is True):
            errs.append(f"{where}: rule-mode binding needs an eval_ref or requires_calibrated (tightening-only bindings are exempt)")
        if mode == "expected_utility" and w.get("requires_calibrated") is not True:
            errs.append(f"{where}: expected_utility needs requires_calibrated: true")
        # effort: the primary member of every reachable profile must accept it (fallback members degrade with a degradation)
        for gid in reachable_generation_profiles(cfg, rule):
            members = gp[gid]["members"]
            if then.get("effort"):
                primary = models.get(members[0]["model"])
                if primary is not None and then["effort"] not in primary["controls"]["effort"]:
                    errs.append(f"{where}: effort '{then['effort']}' is not accepted by the primary member {primary['id']} of reachable profile '{gid}'")
            if then.get("max_output_tokens"):
                for mem in members:
                    m = models.get(mem["model"]) or {}
                    cap = m.get("max_output_tokens_cap")
                    if cap and then["max_output_tokens"] + m.get("reasoning_reserve_tokens", 0) > cap:
                        errs.append(f"{where}: max_output_tokens {then['max_output_tokens']} + reasoning reserve exceeds {m['id']} cap {cap} (profile '{gid}')")
    ver = routing.get("verification")
    if ver and ver["decision_profile"] not in dp:
        errs.append("routing.verification: unknown decision profile")
    elif ver:
        vq = {q["name"] for q in dp[ver["decision_profile"]].get("questions", [])}
        for name in ver.get("questions", []):
            if name not in vq:
                errs.append(f"routing.verification: question '{name}' not defined in profile {ver['decision_profile']}")
    for gid, g in gp.items():
        sel = g.get("selector")
        if sel and sel["decision_profile"] in dp:
            sq = {q["name"]: q for q in dp[sel["decision_profile"]].get("questions", [])}
            if sel["question"] not in sq:
                errs.append(f"generation_profiles.{gid}.selector: question '{sel['question']}' not defined")
            elif sq[sel["question"]].get("dynamic_choices") != "candidates":
                errs.append(f"generation_profiles.{gid}.selector: question must use dynamic_choices: candidates")
    sp = routing["speculation"]
    rp = dp.get(routing["router_profile"])
    if rp and sp["holdback_ms"] < rp["deadline_ms"]:
        errs.append("routing.speculation.holdback_ms must be >= the router profile deadline")
    if sp.get("commit_on_quorum") and rp:
        errs += early_commit_grammar_errors(cfg, rp)

    for alias in cfg["labeling"]["labelers"]:
        need_model(alias, "labeler", "labeling.labelers")
    acc_schema = SCHEMA["$defs"]["Labeling"]["properties"]["acceptance"]["properties"]
    acc = {k: cfg["labeling"].get("acceptance", {}).get(k, v["default"]) for k, v in acc_schema.items()}  # absent = schema default
    if acc["drift_ece_max"] < acc["ece_max"]:  # P1-C1 (IMPL-039); the contract bounds themselves are schema bounds
        errs.append(f"labeling.acceptance: drift_ece_max {acc['drift_ece_max']} must be >= ece_max {acc['ece_max']}")
    for cls in ("confidential", "restricted"):  # P1-C4 (IMPL-038): L2 holds public and internal data only (04:L160)
        if cfg.get("platform", {}).get("data_classes", {}).get(cls, {}).get("l2_cache") is True:
            errs.append(f"platform.data_classes.{cls}.l2_cache must not be true (L2 is for public and internal data only)")

    for tid, t in cfg["tenants"].items():
        for alias in t.get("allowed_models", []):
            if alias not in models:
                errs.append(f"tenants.{tid}.allowed_models: unknown model '{alias}'")
        d = t.get("defaults", {})
        if d.get("decision_profile") and d["decision_profile"] not in dp:
            errs.append(f"tenants.{tid}.defaults.decision_profile unknown")
        if d.get("generation_profile") and d["generation_profile"] not in gp:
            errs.append(f"tenants.{tid}.defaults.generation_profile unknown")
        ro = t.get("request_overrides", {})
        for p in ro.get("decision_profiles", []):
            if p not in dp:
                errs.append(f"tenants.{tid}.request_overrides: unknown decision profile '{p}'")
        for p in ro.get("generation_profiles", []):
            if p not in gp:
                errs.append(f"tenants.{tid}.request_overrides: unknown generation profile '{p}'")
        if t["invoke_role_arn"].split(":")[4] != t["aws_account_id"]:
            errs.append(f"tenants.{tid}: invoke_role_arn account != aws_account_id")
    return errs


def _is_selector(cfg, pid):
    """Profiles used to judge answers (selector / verification) need 'selector' role members."""
    routing = cfg["routing"]
    if routing.get("verification", {}).get("decision_profile") == pid:
        return True
    return any(g.get("selector", {}).get("decision_profile") == pid for g in cfg["generation_profiles"].values())


def check(cfg):
    errs = [f"schema: {'/'.join(map(str, e.absolute_path))}: {e.message[:120]}" for e in VALIDATOR.iter_errors(cfg)]
    return errs or semantic_errors(cfg)


NEGATIVE = {
    "unknown model in ensemble": lambda c: c["decision_profiles"]["router-default"]["members"][0].update(model="kimi-k3"),
    "logprobs elicitation on a model without logprobs": lambda c: c["decision_profiles"]["router-default"]["members"][2].update(elicitation="token_logprobs"),
    "sampling on a model that rejects temperature": lambda c: c["decision_profiles"]["router-default"]["members"][2].update(elicitation="sampling", samples=5),
    "unsupported effort value": lambda c: c["generation_profiles"]["sol-fast"]["members"][0].update(effort="none"),
    "quorum larger than members": lambda c: c["decision_profiles"]["router-default"].update(min_quorum=4),
    "escalation cycle": lambda c: c["decision_profiles"]["router-plus"].update(escalation={"to_profile": "router-default", "when": {"unavailable": True}}),
    "best_of_n without selector": lambda c: c["generation_profiles"]["best-of-sol-opus"].pop("selector"),
    "best_of_n streaming live": lambda c: c["generation_profiles"]["best-of-sol-opus"].update(streaming="live"),
    "decision model used for generation": lambda c: c["generation_profiles"]["sol-fast"]["members"][0].update(model="haiku-5.5"),
    "rule binding without eval_ref": lambda c: c["routing"]["bindings"][0]["when"].pop("eval_ref"),
    "labeling not unanimous": lambda c: c["labeling"].update(rule="majority"),
    "role ARN in another account": lambda c: c["tenants"]["t_acme"].update(invoke_role_arn="arn:aws:iam::999999999999:role/AghooBedrockInvoke"),
    "intent route to unknown profile": lambda c: c["routing"]["intent_routes"].update(coding="gpt-oss-route"),
    "commit-critical question not in profile": lambda c: c["decision_profiles"]["router-default"]["commit_critical"].append("mood"),
    "verification question undefined": lambda c: c["routing"]["verification"]["questions"].append("tone_ok"),
    "selector question without dynamic choices": lambda c: c["decision_profiles"]["answer-verifier"]["questions"][2].pop("dynamic_choices"),
    "holdback shorter than decision deadline": lambda c: c["routing"]["speculation"].update(holdback_ms=500),
    "intent route key not an intent choice": lambda c: c["routing"]["intent_routes"].update(codign="opus-deep"),
    # Revision 5 (F04): each case must be rejected for the stated reason, not just rejected
    "binding refers to an undefined question": (lambda c: c["routing"]["bindings"][0]["when"].update(answer="intennt"), "undefined platform question"),
    "binding names an undeclared score level": (lambda c: c["routing"]["bindings"][2]["when"].update(level_in=["impossible_level"]), "is not a level"),
    "binding names an undeclared choice value": (lambda c: c["routing"]["bindings"][0]["when"].update(value_in=["not_an_intent"]), "is not a value"),
    "threshold binding without mode and evidence": (lambda c: [c["routing"]["bindings"][0]["when"].pop(k) for k in ("mode", "eval_ref")], "needs an eval_ref"),
    "expected_utility without calibration": (lambda c: c["routing"]["bindings"][0]["when"].update(mode="expected_utility", requires_calibrated=False), "schema"),
    "malformed route expression": (lambda c: c["routing"]["bindings"][0]["then"].update(generation_profile="intent_routes[missing_answer"), "malformed route expression"),
    "route expression for another answer": (lambda c: c["routing"]["bindings"][0]["then"].update(generation_profile="intent_routes[complexity]"), "must reference"),
    "duplicate decision member": (lambda c: c["decision_profiles"]["router-default"]["members"].append(copy.deepcopy(c["decision_profiles"]["router-default"]["members"][0])), "duplicate member"),
    "binding output budget above every model cap": (lambda c: c["routing"]["bindings"][2]["then"].update(max_output_tokens=1_000_000), "exceeds"),
    "binding budget plus reasoning reserve above cap": (lambda c: (c["models"]["glm-5.3"].update(reasoning_reserve_tokens=500), c["routing"]["bindings"][2]["then"].update(max_output_tokens=4000)), "reasoning reserve"),
    "all member weights zero": (lambda c: [m.update(weight=0) for m in c["decision_profiles"]["router-default"]["members"]], "sum to 0"),
    "binding effort not accepted by the primary member": (lambda c: c["routing"]["bindings"][3]["then"].update(effort="none"), "primary member"),
    "request-source binding with a reserved router name": (lambda c: c["routing"]["bindings"].append({"when": {"source": "request", "answer": "intent", "answer_type": "choice", "value_in": ["x"]}, "then": {"effort": "low"}}), "reserved router name"),
    "early commit without proof_ref": (lambda c: c["routing"]["speculation"].update(commit_on_quorum=True), "proof_ref"),
    "early commit with a calibrated commit-critical binding": (lambda c: (c["routing"]["speculation"].update(commit_on_quorum=True, proof_ref="rpt_early_commit_property_test_v1"), c["routing"]["bindings"][0]["when"].update(requires_calibrated=True)), "cannot certify a calibrated binding"),
    "early commit with overlapping direct and low regions": (lambda c: (c["routing"]["speculation"].update(commit_on_quorum=True, proof_ref="rpt_early_commit_property_test_v1"), c["routing"]["bindings"][0]["when"].update(p_max_gte=0.3)), "overlaps the low band"),
    "early commit with a score band binding": (lambda c: (c["routing"]["speculation"].update(commit_on_quorum=True, proof_ref="rpt_early_commit_property_test_v1"), c["routing"]["bindings"].append({"when": {"answer": "complexity", "confidence_band": "low", "eval_ref": "rpt_x"}, "then": {"effort": "low"}})), "not convex"),
    "member template missing from the registry": (lambda c: c["decision_profiles"]["router-default"]["members"][0].update(template="logprob-codes@9"), "unknown template"),
    "template elicitation differs from the member": (lambda c: c["decision_profiles"]["router-default"]["members"][1].update(template="logprob-codes@2"), "member elicitation"),
    "logprob template above top_logprobs minus reserve": (lambda c: c["templates"]["logprob-codes@2"].update(max_choices=18), "reserve"),
    "logprob template above 16": (lambda c: (c["models"]["deepseek-3.2"]["controls"].update(top_logprobs_max=20), c["templates"]["logprob-codes@2"].update(max_choices=17)), "above the token_logprobs limit"),
    "verbalized template above 26 letters": (lambda c: c["templates"]["verbalized-dist@1"].update(max_choices=27), "above the verbalized limit"),
    "commit-critical question above the smallest member limit": (lambda c: c["decision_profiles"]["router-default"]["questions"][0]["choices"].extend({"value": f"extra_{i}"} for i in range(7)), "commit-critical question"),
    "backup request that cannot land before the deadline": (lambda c: c["decision_profiles"]["router-default"]["members"][0].update(backup_after_ms=900), "could not land in time"),
    "backup request without a p95 latency": (lambda c: c["decision_profiles"]["router-default"]["members"][0].pop("latency_p95_ms"), "latency_p95_ms"),
    "labeling without a family-overlap lift threshold": (lambda c: c["labeling"].pop("family_overlap_lift_max"), "family_overlap_lift_max"),
    "request-source binding without answer_type": (lambda c: c["routing"]["bindings"].append({"when": {"source": "request", "answer": "tone", "value_in": ["x"]}, "then": {"effort": "low"}}), "schema"),
    # Revision 6 (spec-08 §14.4, spec-05 §3): P1-C1 to P1-C4
    "drift ECE threshold below the acceptance ECE": (lambda c: c["labeling"].update(acceptance={"drift_ece_max": 0.04}), "drift_ece_max 0.04 must be >= ece_max 0.05"),
    "confidence bands with low_lt equal to high_gte": (lambda c: c["routing"]["confidence_bands"].update(low_lt=0.75), "low_lt must be < high_gte"),
    "escalation question not in the source profile": (lambda c: c["decision_profiles"]["router-default"]["escalation"]["questions"].append("mood"), "escalation.questions: 'mood'"),
    "L2 cache enabled for restricted data": (lambda c: c.update(platform={"data_classes": {"restricted": {"l2_cache": True}}}), "restricted.l2_cache must not be true"),
}

POSITIVE = {  # mutations that must stay valid
    "early commit with proof_ref and the restricted grammar": lambda c: c["routing"]["speculation"].update(
        commit_on_quorum=True, proof_ref="rpt_early_commit_property_test_v1"),
}

if __name__ == "__main__":
    paths = [pathlib.Path(p) for p in sys.argv[1:]] or [HERE / "examples" / "platform.example.yaml"]
    failures = 0
    for path in paths:
        cfg = yaml.safe_load(path.read_text())
        errs = check(cfg)
        print(("PASS" if not errs else "FAIL") + f"  {path.name} " + ("valid" if not errs else f"-> {errs}"))
        failures += bool(errs)
        for name, entry in NEGATIVE.items():
            mutate, expect = entry if isinstance(entry, tuple) else (entry, None)
            bad = copy.deepcopy(cfg)
            mutate(bad)
            rejected = check(bad)
            ok = bool(rejected) and (expect is None or any(expect in e for e in rejected))
            failures += not ok
            print(f"{'PASS' if ok else 'FAIL'}  negative: {name:48s} " + (f"rejected: {rejected[0][:90]}" if ok else f"NOT rejected for the expected reason ({expect}): {rejected}"))
        for name, mutate in POSITIVE.items():
            good = copy.deepcopy(cfg)
            mutate(good)
            errs = check(good)
            failures += bool(errs)
            print(f"{'PASS' if not errs else 'FAIL'}  positive: {name:48s} " + ("valid" if not errs else f"-> {errs}"))
    print(f"\n{failures} unexpected outcome(s)")
    sys.exit(1 if failures else 0)
