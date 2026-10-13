"""Regenerate contract examples (reproducible). Run: python3 -I make_examples.py

The `department` answer uses real member outputs from the Bedrock probes (2026-10-09):
  - deepseek.v3.2 token logprobs           probes/results/bedrock_selection.json  (mantle_logprobs_deepseek.v3.2)
  - us.openai.gpt-6-luna verbalized         probes/results/bedrock_luna6.json      (outputConfig.effort=none, ambiguous)
  - us.anthropic.claude-haiku-5-5 verbalized probes/results/bedrock_verbalized.json (ambiguous)
Everything else (needs_human, urgency, calibration metrics and binding, escalation stage) is illustrative.
The hierarchical example (lightweight-model-decide, 32 options) takes its stage-1 group distribution from the real P13 gpt-6-luna output
(probes/results/bedrock_luna6_jev.json); its stage-2 distributions, two-round latency and usage are illustrative.
The censored example (spec-06 T-LP-1 top list), the Jev K = 16 pair and the facade non-pattern-name pair are illustrative.
"""
import copy
import hashlib
import hmac
import json
import math
import pathlib

HERE = pathlib.Path(__file__).parent
EX = HERE / "examples"
PROBES = HERE.parents[2] / "probes" / "results"
BANDS = {"high_gte": 0.75, "low_lt": 0.4}  # routing.confidence_bands in examples/platform.example.yaml
r4 = lambda x: round(x, 4)
OPTS = ["billing", "technical", "shipping", "other"]
CODE = dict(zip("ABCD", OPTS))
TENANT_KEY = b"example-tenant-key"
MEMBERS = [  # router-default@1 order (config)
    ("m1", "bedrock-mantle-chat", "deepseek.v3.2", "token_logprobs", "logprob-codes@2"),
    ("m2", "bedrock-converse", "us.openai.gpt-6-luna", "verbalized", "verbalized-dist@1"),
    ("m3", "bedrock-converse", "us.anthropic.claude-haiku-5-5", "verbalized", "verbalized-dist@1"),
]
LAT = {"m1": 488, "m2": 883, "m3": 938}
CALL_USAGE = {"m1": (60, 2), "m2": (120, 29), "m3": (130, 35)}  # per question (input, output), from probe usage scale


def sha(s):
    return "sha256:" + hashlib.sha256(s.encode()).hexdigest()


def usage(inp, out, cost):
    return {"input_tokens": inp, "output_tokens": out, "cached_input_tokens": 0, "cache_write_input_tokens": 0, "reasoning_tokens": 0,
            "cost_usd": cost, "cost_estimated": True, "tokens_estimated": False}


def band(value):
    return "high" if value >= BANDS["high_gte"] else "low" if value < BANDS["low_lt"] else "medium"


def renorm(d, keys):
    s = sum(d[k] for k in keys)
    return {k: d[k] / s for k in keys}


def js(p, q, keys):
    mix = {k: (p[k] + q[k]) / 2 for k in keys}
    kl = lambda a, b: sum(a[k] * math.log2(a[k] / b[k]) for k in keys if a[k] > 0)
    return (kl(p, mix) + kl(q, mix)) / 2


def argmax(d, keys):
    return max(keys, key=lambda k: (d[k], -keys.index(k)))


def scored(name, qtype, keys, member_dists, labels=None, calibrated=None, view=None, members_cfg=None, quorum_required=2):
    """Build a scored answer from member distributions (linear pool, equal weights)."""
    MEMBERS_ = members_cfg or MEMBERS
    view = view or {m[0]: "native" for m in MEMBERS_}
    used = [m for m in MEMBERS_ if view[m[0]] != "skipped"]
    pool = {k: sum(member_dists[m[0]][k] for m in used) / len(used) for k in keys}
    prob = calibrated or pool
    order = sorted(keys, key=lambda k: (-round(prob[k], 4), keys.index(k)))
    votes = {k: sum(1 for m in used if argmax(member_dists[m[0]], keys) == k) for k in keys}
    top = order[0]
    H = -sum(p * math.log(p) for p in prob.values() if p > 0) / math.log(len(keys))
    if qtype == "score":
        idx = {k: i for i, k in enumerate(keys)}
        mode = max(range(len(keys)), key=lambda i: (prob[keys[i]], -i))
        n = len(keys); centre = (n - 1) / 2
        mad_u = sum(abs(i - centre) for i in range(n)) / n
        conf_val, method = max(0.0, 1 - sum(prob[k] * abs(idx[k] - mode) for k in keys) / mad_u), "ordinal_dispersion"
    else:
        n = len(keys)
        conf_val, method = (prob[top] - 1 / n) / (1 - 1 / n), "normalized_max"
    band_basis = prob[top] if calibrated else conf_val
    cands = []
    for k in order:
        c = {"value": (k == "true") if qtype == "predicate" else k, "probability": r4(prob[k]), "raw_score": r4(pool[k]), "votes": votes[k]}
        if qtype == "score":
            c["index"] = keys.index(k)
        cands.append(c)
    diff = round(1 - sum(c["probability"] for c in cands), 4)
    cands[0]["probability"] = r4(cands[0]["probability"] + diff)
    members = []
    for mid, prov, model, elic, _ in MEMBERS_:
        d = member_dists[mid]
        mv = {"member_id": mid, "provider": prov, "model": model, "region": "us-east-1", "elicitation": elic,
              "input_view": view[mid], "status": "skipped" if view[mid] == "skipped" else "ok",
              "weight": r4(1 / len(used)) if view[mid] != "skipped" else 0, "latency_ms": LAT[mid] if view[mid] != "skipped" else 0}
        if view[mid] != "skipped":
            dist = {k: r4(d[k]) for k in keys}
            k0 = max(dist, key=dist.get); dist[k0] = r4(dist[k0] + round(1 - sum(dist.values()), 4))
            mv["distribution"] = dist
            inp, out = CALL_USAGE[mid]
            mv["usage"] = usage(inp, out, 0.00002)
        members.append(mv)
    ans = {"type": qtype, "name": name,
           "candidates": cands,
           "calibration": {"status": "uncalibrated", "source": "mixed", "member_subset": [m[0] for m in used]},
           "confidence": {"kind": "heuristic", "value": r4(conf_val), "method": method, "band": band(band_basis),
                          "margin": r4(prob[order[0]] - prob[order[1]]), "normalized_entropy": r4(H),
                          "agreement": r4(votes[top] / len(used)),
                          "member_divergence": r4(sum(js(member_dists[m[0]], pool, keys) for m in used) / len(used))},
           "quorum": {"required": quorum_required, "responded": len(used), "refused": 0, "total": len(MEMBERS_)},
           "members": members}
    if qtype == "choice":
        ans["choice"] = cands[0]["value"]
    if qtype == "predicate":
        ans["probability"] = next(c["probability"] for c in cands if c["value"] is True)
    if qtype == "score":
        ans["score"] = r4(sum(c["index"] * c["probability"] for c in cands))
        ans["level"] = cands[0]["value"]
    return ans


# --- hierarchical choice example (choice cardinality, 08 §3.4) -----------------------------------------------------------
H_GROUPS = {  # 4 request-supplied groups x 8 options = 32 options, above the 26-letter limit of verbalized-dist@1
    "billing": ["double_charge", "refund_status", "invoice_copy", "wrong_amount", "failed_payment", "subscription_cancel", "promo_code", "tax_question"],
    "technical": ["login_failure", "app_crash", "export_error", "sync_problem", "api_error", "slow_performance", "data_missing", "integration_setup"],
    "shipping": ["not_delivered", "marked_delivered_not_received", "damaged_item", "wrong_item", "delayed", "address_change", "tracking_missing", "customs_hold"],
    "other": ["feedback", "account_deletion", "partnership", "press", "privacy_request", "accessibility", "job_inquiry", "unclear"],
}
H_OPTS = [f"{g}_{o}" for g, opts in H_GROUPS.items() for o in opts]
# Stage 2 (illustrative, no probe exists): P(option | group) for the two groups stage 1 expands. Each sums to 1.
H_STAGE2 = {
    "billing": dict(zip(H_GROUPS["billing"], [.55, .08, .04, .15, .12, .03, .02, .01])),
    "shipping": dict(zip(H_GROUPS["shipping"], [.20, .60, .03, .02, .05, .01, .08, .01])),
}


# A dominant one-option group (spec-06 §19.6, ruling R1, illustrative): `fraud` holds one option and 0.93 of stage 1, so E keeps
# growing in top-mass order until it holds >= 2 options: E = {fraud, billing}, 9 candidates, unresolved 0.03.
H1_GROUPS = {"fraud": ["report"], **H_GROUPS}
H1_STAGE2 = {"fraud": {"report": 1.0}, **H_STAGE2}
H1_STAGE1 = [.93, .04, .01, .015, .005]


def hierarchical_answer(stage1, expanded, name="issue", hgroups=H_GROUPS, stage2=H_STAGE2):
    """Option probability = P(group) * P(option | group) for expanded groups; the rest is unresolved_mass (never spread)."""
    gnames = list(hgroups)
    opts = [f"{g}_{o}" for g, members in hgroups.items() for o in members]
    groups = [{"id": f"g{i + 1}", "label": g, "options": [f"{g}_{o}" for o in hgroups[g]], "probability": r4(stage1[i]),
               "expanded": g in expanded} for i, g in enumerate(gnames)]
    joint = {f"{g}_{o}": stage1[gnames.index(g)] * p for g in expanded for o, p in stage2[g].items()}
    unresolved = r4(sum(stage1[i] for i, g in enumerate(gnames) if g not in expanded))
    order = sorted(joint, key=lambda o: (-round(joint[o], 4), opts.index(o)))
    dist = {o: r4(joint[o]) for o in order}
    dist[order[0]] = r4(dist[order[0]] + round(1 - sum(dist.values()) - unresolved, 4))
    n = len(order) + 1  # the unresolved mass is one extra bucket
    conf = (joint[order[0]] - 1 / n) / (1 - 1 / n)          # unrounded values, as the server computes them
    buckets = list(joint.values()) + [unresolved]
    H = -sum(p * math.log(p) for p in buckets if p > 0) / math.log(n)
    ans = {"type": "choice", "name": name, "choice": order[0], "choice_method": "hierarchical", "unresolved_mass": unresolved, "groups": groups,
           "candidates": [{"value": o, "probability": dist[o], "raw_score": dist[o], "votes": int(o == order[0])} for o in order],
           "calibration": {"status": "uncalibrated", "source": "verbalized", "member_subset": ["m1"]},
           "confidence": {"kind": "heuristic", "value": r4(conf), "method": "normalized_max", "band": band(conf),
                          "margin": r4(joint[order[0]] - joint[order[1]]), "normalized_entropy": r4(H), "agreement": 1.0, "member_divergence": None},
           "quorum": {"required": 1, "responded": 1, "refused": 0, "total": 1},
           "members": [{"member_id": "m1", "provider": "bedrock-converse", "model": "us.openai.gpt-6-luna", "region": "us-east-1",
                        "elicitation": "verbalized", "input_view": "native", "status": "ok", "distribution": dist, "unresolved_mass": unresolved,
                        "weight": 1.0, "latency_ms": 1430, "usage": usage(430, 90, 0.00006)}]}
    return ans


# --- Jev with K = 16 (IMPL-049, illustrative) ------------------------------------------------------------------------------
# Native emitted 4-dp values of a router-default answer: a 16-option choice whose pool keeps a censored-tail unresolved_mass
# (native confidence uses n = K + 1), and a 16-level score. Their 2-dp Jev maps drift from them, so every Jev number is computed
# from the map, never copied from the native answer.
K16_CHOICE = {"accounts": .0154, "billing": .0143, "checkout": .0118, "catalog": .0132, "search": .0349, "recommendations": .0069,
              "notifications": .0255, "mobile_app": .0196, "web_app": .0644, "api": .1187, "integrations": .0835, "reporting": .3046,
              "exports": .2213, "permissions": .0451, "security": .0105, "other": .0091}
K16_CHOICE_UNRESOLVED = .0012
K16_SCORE = [.0143, .0141, .0146, .0139, .0148, .0242, .0238, .0341, .0447, .0656, .0951, .1548, .2146, .1257, .0753, .0704]


def jev_answer(q, probs):
    """Jev answer from native emitted probabilities (spec-06 §12.2): the 2-dp map; choice, score and confidence from that map."""
    if q["type"] == "choice":
        values = [c["value"] for c in q["choices"]]
        m = {v: round(probs[v], 2) for v in values}
        top = max(values, key=lambda v: (m[v], -values.index(v)))
        k = len(values)
        return {"type": "choice", "choice": top, "probabilities": m, "confidence": round((m[top] - 1 / k) / (1 - 1 / k), 2)}
    pm = [round(x, 2) for x in probs]
    k = len(pm)
    mode = max(range(k), key=lambda i: (pm[i], -i))
    mad_u = sum(abs(i - (k - 1) / 2) for i in range(k)) / k
    return {"type": "score", "score": round(sum(i * x for i, x in enumerate(pm)), 2),
            "legend": {str(i): lv["label"] if lv.get("description") is None else lv["description"] for i, lv in enumerate(q["levels"])},
            "probabilities": {str(i): x for i, x in enumerate(pm)},
            "confidence": round(max(0.0, 1 - sum(x * abs(i - mode) for i, x in enumerate(pm)) / mad_u), 2)}


def main():
    sel = json.loads((PROBES / "bedrock_selection.json").read_text())
    ver = json.loads((PROBES / "bedrock_verbalized.json").read_text())["results"]
    ds = dict(sel["mantle_logprobs_deepseek.v3.2"]["answer_dist"])
    luna = json.loads((PROBES / "bedrock_luna6.json").read_text())["verbalized"]["outputConfig.effort=none ambiguous"][0]["dist"]
    haiku = ver["us.anthropic.claude-haiku-5-5 effort=default structured=False ambiguous"][0]["dist"]
    dept = {"m1": renorm({CODE[k]: ds[k] for k in "ABCD"}, OPTS),
            "m2": renorm({CODE[k]: v for k, v in luna.items()}, OPTS),
            "m3": renorm({CODE[k]: v for k, v in haiku.items()}, OPTS)}
    hum = {"m1": {"true": .03, "false": .97}, "m2": {"true": .05, "false": .95}, "m3": {"true": .085, "false": .915}}
    urg = {"m1": {"low": .15, "medium": .65, "high": .20}, "m2": {"low": .05, "medium": .78, "high": .17},
           "m3": {"low": .10, "medium": .658, "high": .242}}
    questions = [
        {"type": "choice", "name": "department", "instructions": "Which team should handle this message?",
         "choices": [{"value": "billing", "description": "Payments, refunds, invoices"}, {"value": "technical", "description": "Bugs and errors"},
                     {"value": "shipping", "description": "Delivery problems"}, {"value": "other", "description": None}]},
        {"type": "predicate", "name": "needs_human", "instructions": "The customer explicitly asks to speak with a human.",
         "criteria": {"true": "Asks for a person, agent or call back", "false": "No such request"}},
        {"type": "score", "name": "urgency", "instructions": "How urgent is the request?",
         "levels": [{"label": "low", "description": "Can wait days"}, {"label": "medium", "description": None},
                    {"label": "high", "description": "Blocking now"}]},
    ]
    text = "My package says delivered but I was also charged twice."
    req = {"profile": "router-default@1", "input": text, "questions": questions, "deadline_ms": 1000, "min_quorum": 2,
           "include": ["members"], "safety_identifier": "user-7f3a"}
    a_dept = scored("department", "choice", OPTS, dept)
    a_hum = scored("needs_human", "predicate", ["true", "false"], hum)
    # urgency: calibrated (illustrative): single pooled temperature T=1.4 maps raw pool -> calibrated
    pool_u = {k: sum(urg[m][k] for m in urg) / 3 for k in ["low", "medium", "high"]}
    T = 1.4
    z = {k: pool_u[k] ** (1 / T) for k in pool_u}; s = sum(z.values())
    a_urg = scored("urgency", "score", ["low", "medium", "high"], urg, calibrated={k: v / s for k, v in z.items()})
    views = ",".join(f"{m[0]}:native" for m in MEMBERS)
    # The whole holdout and binding below are illustrative (no calibration artifact exists yet); they only exercise the contract.
    a_urg["calibration"] = {"status": "calibrated", "source": "mixed", "method": "temperature", "calibrator_id": "cal_urgency_v3",
                            "member_subset": ["m1", "m2", "m3"],
                            "binding": {"profile_id": "router-default", "profile_version": "1",
                                        "question_signature": sha(json.dumps(questions[2], sort_keys=True, separators=(",", ":"))),
                                        "input_view_signature": sha(views), "choice_method": "flat"},
                            "holdout": {"dataset_id": "ds_tenant42_urgency_2026q4", "label_source": "llm_consensus",
                                        "labelers": ["us.openai.gpt-6.1-sol", "us.anthropic.claude-sonnet-5-5"], "consensus_rate": 0.81,
                                        "consensus_rate_by_class": {"low": 0.86, "medium": 0.77, "high": 0.83},
                                        "n": 1200, "n_by_class": {"low": 330, "medium": 600, "high": 270},
                                        "ece": 0.031, "brier": 0.284, "nll": 0.71, "nll_uncalibrated": 0.78, "flagged_classes": [],
                                        "scope": {"target": "llm_consensus_agreement", "retained_share": 0.78,
                                                  "slices": [{"name": "input_view:text_only", "n": 1200}, {"name": "language:en", "n": 1140}]},
                                        "family_overlap": {"max_member_lift": 0.04, "threshold": 0.10},
                                        "evaluated_at": "2026-10-01T12:00:00Z"}}
    total_in = sum(CALL_USAGE[m][0] for m in CALL_USAGE) * 3
    total_out = sum(CALL_USAGE[m][1] for m in CALL_USAGE) * 3
    res = {"id": "dec_01J9ZQ6M3K8V", "object": "decision", "created_at": 1791504300, "status": "completed",
           "answers": [a_dept, a_hum, a_urg],
           "provenance": {"trace_id": "4bf92f3577b34da6a3ce929d0e0e4736", "profile_id": "router-default", "profile_version": "1",
                          "question_signature": sha(json.dumps(questions, sort_keys=True, separators=(",", ":"))),
                          "input_digest": "hmac-sha256:" + hmac.new(TENANT_KEY, text.encode(), hashlib.sha256).hexdigest(),
                          "input_view_signature": sha(views), "option_order_seed": 1791504300,
                          "prompt_templates": {m[0]: m[4] for m in MEMBERS},
                          "aggregation": {"method": "linear_pool"}, "deadline_ms": 1000,
                          "quorum": {"required": 2, "responded": 3, "refused": 0, "total": 3}, "cache": {"hit": False}, "stages": [],
                          "started_at": "2026-10-09T00:05:00.000Z", "completed_at": "2026-10-09T00:05:00.945Z"},
           "usage": usage(total_in, total_out, 0.00018)}
    EX.mkdir(exist_ok=True)
    for f in EX.glob("decision-*.json"):
        f.unlink()
    for f in EX.glob("openai-*.json"):
        f.unlink()
    w = lambda name, obj: (EX / name).write_text(json.dumps(obj, indent=2) + "\n")
    w("decision-request.valid.json", req)
    w("decision-result.valid.json", res)
    w("decision-request.valid-json-state.json", {"profile": "router-default", "input": {"order_id": "A-17", "events": ["delivered", "charged", "charged"]},
                                                 "questions": questions[:2]})
    w("decision-request.valid-default-profile.json", {k: v for k, v in req.items() if k != "profile"})  # tenant defaults.decision_profile answers

    # image turn + escalation (illustrative): deepseek is text-only -> sees OCR text; department escalated to router-plus
    img_req = {"profile": "router-default@1", "questions": questions[:1], "include": ["members", "stages"],
               "input": [{"role": "user", "content": [{"type": "input_text", "text": "Which team handles this?"},
                                                      {"type": "input_image", "file_id": "file_01J9ZR2Q"}]}]}
    plus = MEMBERS  # router-plus: same members, higher effort, all three must answer
    img_view = {"m1": "ocr_text", "m2": "native", "m3": "native"}
    esc = scored("department", "choice", OPTS,
                 {"m1": {"billing": .55, "technical": .05, "shipping": .30, "other": .10},
                  "m2": {"billing": .70, "technical": .02, "shipping": .25, "other": .03},
                  "m3": {"billing": .62, "technical": .03, "shipping": .30, "other": .05}},
                 view=img_view, members_cfg=plus, quorum_required=3)
    esc_res = copy.deepcopy(res)
    esc_res.update(id="dec_01J9ZR3T", answers=[esc])
    p = esc_res["provenance"]
    p["prompt_templates"] = {m[0]: m[4] for m in plus}
    p["quorum"] = {"required": 3, "responded": 3, "refused": 0, "total": 3}
    p.update(input_view_signature=sha(",".join(f"{k}:{v}" for k, v in img_view.items())), started_at="2026-10-09T00:06:00.000Z",
             completed_at="2026-10-09T00:06:03.410Z", deadline_ms=1000,
             stages=[{"profile_id": "router-plus", "profile_version": "1", "triggered_by": ["confidence_below"], "questions": ["department"],
                      "usage": usage(520, 120, 0.00009)}])
    p["stages"][0].update(input_view_signature=p["input_view_signature"], prompt_templates=dict(p["prompt_templates"]))  # CP-DEC-2: the stage's own views and templates
    p["question_signature"] = sha(json.dumps(questions[:1], sort_keys=True, separators=(",", ":")))
    esc_res["usage"] = usage(330 + 520, 66 + 120, 0.00015)
    w("decision-request.valid-image.json", img_req)
    w("decision-result.valid-escalated.json", esc_res)
    no_view = copy.deepcopy(esc_res)  # every member only saw OCR text of the image: no adequate view, so the answer must be unavailable
    for m in no_view["answers"][0]["members"]:
        m["input_view"] = "ocr_text"
    w("decision-result.invalid-image-no-adequate-view.json", no_view)

    bad = lambda f: (lambda r: (f(r), r)[1])(copy.deepcopy(res))
    invalid = {
        "calibrated-without-holdout": lambda r: r["answers"][2].__setitem__("calibration", {"status": "calibrated", "method": "temperature"}),
        "choice-not-argmax": lambda r: r["answers"][0].__setitem__("choice", "other"),
        "probabilities-not-normalized": lambda r: r["answers"][0]["candidates"][1].__setitem__("probability", 0.6),
        "confidence-labeled-probability": lambda r: r["answers"][1]["confidence"].__setitem__("kind", "probability"),
        "score-not-expected-index": lambda r: r["answers"][2].__setitem__("score", 1.5),
        "consensus-labels-without-labelers": lambda r: r["answers"][2]["calibration"]["holdout"].pop("labelers"),
        "calibrated-below-acceptance": lambda r: r["answers"][2]["calibration"]["holdout"].update(ece=0.21),
        "timing-shorter-than-member": lambda r: r["provenance"].__setitem__("completed_at", "2026-10-09T00:05:00.640Z"),
        "answer-quorum-mismatch": lambda r: r["answers"][0]["quorum"].__setitem__("responded", 2),
        "answer-not-in-request": lambda r: r["answers"][1].__setitem__("name", "sentiment"),
        "usage-cache-exceeds-input": lambda r: r["usage"].update(cached_input_tokens=r["usage"]["input_tokens"], cache_write_input_tokens=64),
        "calibrated-binding-stage-mismatch": lambda r: r["answers"][2]["calibration"]["binding"].update(profile_id="lightweight-model-decide"),
        "calibrated-class-consensus-below-minimum": lambda r: r["answers"][2]["calibration"]["holdout"]["consensus_rate_by_class"].update(medium=0.41),
        "calibrated-family-overlap-lift": lambda r: r["answers"][2]["calibration"]["holdout"]["family_overlap"].update(max_member_lift=0.23),
        "calibrated-nll-worse-than-uncalibrated": lambda r: r["answers"][2]["calibration"]["holdout"].update(nll=0.83),
        "wrong-confidence-method": lambda r: r["answers"][2]["confidence"].__setitem__("method", "normalized_max"),
        "band-inconsistent": lambda r: r["answers"][0]["confidence"].__setitem__("band", "high"),
        "human-label-source": lambda r: r["answers"][2]["calibration"]["holdout"].__setitem__("label_source", "human"),
    }
    for name, f in invalid.items():
        w(f"decision-result.invalid-{name}.json", bad(f))
    w("decision-request.invalid-duplicate-choices.json",
      {**req, "questions": [{**questions[0], "choices": questions[0]["choices"] + [{"value": "billing"}]}]})

    # Jev-shaped output (output_format: jev) from gpt-6-luna alone (profile lightweight-model-decide), real outputs from P13
    jev_probe = json.loads((PROBES / "bedrock_luna6_jev.json").read_text())
    amb = jev_probe["results"]["ambiguous"]
    jev_req = {"profile": "lightweight-model-decide@1", "output_format": "jev", "input": jev_probe["inputs"]["ambiguous"],
               "questions": [
                   {"type": "choice", "name": "department", "instructions": "Which team should handle this message?",
                    "choices": [{"value": "billing", "description": "Payments, refunds, invoices"}, {"value": "technical", "description": "Bugs and errors"},
                                {"value": "shipping", "description": "Delivery problems"}, {"value": "other", "description": None}]},
                   {"type": "score", "name": "urgency", "instructions": "How urgent is the request?",
                    "levels": [{"label": "low", "description": "Can wait days"}, {"label": "medium", "description": "Within a day"},
                               {"label": "high", "description": "Blocking now"}]},
                   {"type": "noul", "name": "needs_human", "instructions": "The customer explicitly asks to speak with a human.",
                    "criteria": {"true": "Asks for a person, agent or call back", "false": "No such request"}}]}
    out_tokens = sum(c.get("output_tokens") or 0 for c in amb["calls"].values())
    jev_res = {"model": "aghoo/lightweight-model-decide@1", "answers": amb["jev_answers"],
               "usage": {"input_tokens": 3 * 110, "output_tokens": out_tokens}, "decision_id": "dec_01J9ZS1L6E"}
    w("decision-request.valid-jev.json", jev_req)
    w("decision-jev-response.valid.json", jev_res)
    def jbad(f):
        b = copy.deepcopy(jev_res); f(b); return b
    w("decision-jev-response.invalid-choice-not-argmax.json", jbad(lambda r: r["answers"]["department"].__setitem__("choice", "other")))
    w("decision-jev-response.invalid-probabilities-not-normalized.json", jbad(lambda r: r["answers"]["department"]["probabilities"].__setitem__("other", 0.3)))
    w("decision-jev-response.invalid-score-not-expected-index.json", jbad(lambda r: r["answers"]["urgency"].__setitem__("score", 2.0)))
    w("decision-jev-response.invalid-legend-keys-mismatch.json", jbad(lambda r: r["answers"]["urgency"]["legend"].pop("2")))
    w("decision-jev-response.invalid-predicate-name-in-jev.json", jbad(lambda r: r["answers"].__setitem__("needs_human", {"type": "predicate", "probability": 0.01})))

    # Jev with K = 16 (illustrative, IMPL-049): router-default carries 16 options flat (logprob-codes@2 max_choices 16)
    k16_questions = [
        {"type": "choice", "name": "product_area", "instructions": "Which product area does the bug report concern?",
         "choices": [{"value": v} for v in K16_CHOICE]},
        {"type": "score", "name": "checklist_items", "instructions": "How many of the 15 bug-report checklist items does the report satisfy?",
         "levels": [{"label": f"{i} of 15", **({0: {"description": "No checklist item"}, 15: {"description": "Every checklist item"}}.get(i, {}))}
                    for i in range(16)]},
    ]
    k16_req = {"profile": "router-default@1", "output_format": "jev", "questions": k16_questions,
               "input": "The weekly revenue report exports an empty CSV since Monday; the dashboard itself shows the numbers. Steps attached."}
    w("decision-request.valid-jev-k16.json", k16_req)
    w("decision-jev-response.valid-k16.json", {"model": "aghoo/router-default@1",
                                                "answers": {"product_area": jev_answer(k16_questions[0], K16_CHOICE),
                                                            "checklist_items": jev_answer(k16_questions[1], K16_SCORE)},
                                                "usage": {"input_tokens": 1830, "output_tokens": 214}, "decision_id": "dec_01J9ZU8K4W"})

    # Hierarchical choice: lightweight-model-decide, 32 options in 4 request-supplied groups. Stage 1 is the real P13 gpt-6-luna distribution
    # (billing .4799 / technical .0201 / shipping .4799 / other .0201); stage 2 and the two-round latency (2 x the measured
    # 0.715 s) and usage are illustrative.
    stage1 = amb["calls"]["department"]["distribution"]
    h_text = jev_probe["inputs"]["ambiguous"]
    h_questions = [{"type": "choice", "name": "issue", "instructions": "Which specific issue is the customer describing?",
                    "choices": [{"value": f"{g}_{o}", "group": g} for g, opts in H_GROUPS.items() for o in opts]}]
    h_req = {"profile": "lightweight-model-decide@1", "input": h_text, "questions": h_questions, "deadline_ms": 3000, "include": ["members"]}
    h_ans = hierarchical_answer(stage1, ["billing", "shipping"])
    h_res = {"id": "dec_01J9ZT2H5N", "object": "decision", "created_at": 1791504500, "status": "completed", "answers": [h_ans],
             "provenance": {"trace_id": "9d1c7e3a5b8f4c2e8a6b0f3d7c1e5a92", "profile_id": "lightweight-model-decide", "profile_version": "1",
                            "question_signature": sha(json.dumps(h_questions, sort_keys=True, separators=(",", ":"))),
                            "input_digest": "hmac-sha256:" + hmac.new(TENANT_KEY, h_text.encode(), hashlib.sha256).hexdigest(),
                            "input_view_signature": sha("m1:native"), "option_order_seed": 1791504500,
                            "prompt_templates": {"m1": "verbalized-dist@1"}, "aggregation": {"method": "linear_pool"}, "deadline_ms": 3000,
                            "quorum": {"required": 1, "responded": 1, "refused": 0, "total": 1}, "cache": {"hit": False}, "stages": [],
                            "started_at": "2026-10-09T00:07:00.000Z", "completed_at": "2026-10-09T00:07:01.452Z"},
             "usage": usage(430, 90, 0.00006)}
    w("decision-request.valid-hierarchical.json", h_req)
    w("decision-result.valid-hierarchical.json", h_res)

    # A dominant one-option group (R1, illustrative): 33 options; `fraud_report` is alone in its group and carries 0.93 of stage 1,
    # so E = {fraud, billing} (>= 2 options) and the answer is scored with 9 candidates.
    h1_text = "Three card payments I never made showed up last night. Please block the card and tell me what happens next."
    h1_questions = [{"type": "choice", "name": "issue", "instructions": "Which specific issue is the customer describing?",
                     "choices": [{"value": f"{g}_{o}", "group": g} for g, opts in H1_GROUPS.items() for o in opts]}]
    h1_req = {**h_req, "input": h1_text, "questions": h1_questions}
    h1_res = copy.deepcopy(h_res)
    h1_res.update(id="dec_01J9ZT3K7Q", answers=[hierarchical_answer(H1_STAGE1, ["fraud", "billing"], hgroups=H1_GROUPS, stage2=H1_STAGE2)])
    h1_res["provenance"].update(trace_id="4e2b8d6f1a3c5e7092b4d6f8a1c3e5b7",
                                question_signature=sha(json.dumps(h1_questions, sort_keys=True, separators=(",", ":"))),
                                input_digest="hmac-sha256:" + hmac.new(TENANT_KEY, h1_text.encode(), hashlib.sha256).hexdigest())
    w("decision-request.valid-hierarchical-lone-group.json", h1_req)
    w("decision-result.valid-hierarchical-lone-group.json", h1_res)

    def hbad(f):
        r = copy.deepcopy(h_res); f(r); return r

    def renormalize(r):  # the censored tail is spread over the resolved options as if it had been measured
        a = r["answers"][0]; scale = 1 / (1 - a["unresolved_mass"])
        for c in a["candidates"]:
            c["probability"] = c["raw_score"] = r4(c["probability"] * scale)
        a["members"][0]["distribution"] = {c["value"]: c["probability"] for c in a["candidates"]}
        a["unresolved_mass"] = a["members"][0]["unresolved_mass"] = 0

    only_billing = copy.deepcopy(h_res); only_billing["answers"] = [hierarchical_answer(stage1, ["billing"])]

    def flat_over_limit(r):  # a 26-letter member "answers" all 32 options flat: unseen options get an invented spread
        a = r["answers"][0]; spread = a["unresolved_mass"] / sum(len(g["options"]) for g in a["groups"] if not g["expanded"])
        dist = {c["value"]: c["probability"] for c in a["candidates"]}
        dist.update({o: spread for g in a["groups"] if not g["expanded"] for o in g["options"]})
        order = sorted(dist, key=lambda o: (-round(dist[o], 4), H_OPTS.index(o)))
        dist = {o: r4(dist[o]) for o in order}; dist[order[0]] = r4(dist[order[0]] + round(1 - sum(dist.values()), 4))
        a.pop("groups"); a["choice_method"] = "flat"; a["unresolved_mass"] = 0; a["choice"] = order[0]
        a["candidates"] = [{"value": o, "probability": dist[o], "raw_score": dist[o], "votes": int(o == order[0])} for o in order]
        a["members"][0].pop("unresolved_mass"); a["members"][0]["distribution"] = dist
        conf = (dist[order[0]] - 1 / 32) / (1 - 1 / 32); a["confidence"].update(value=r4(conf), band=band(conf), margin=r4(dist[order[0]] - dist[order[1]]))

    w("decision-result.invalid-hierarchical-unresolved-renormalized.json", hbad(renormalize))
    w("decision-result.invalid-hierarchical-coverage.json", only_billing)
    w("decision-result.invalid-flat-over-member-limit.json", hbad(flat_over_limit))

    # Censored logprob tail (spec-06 T-LP-1, illustrative): router-default with min_quorum 1; Luna and Haiku were throttled, so DeepSeek
    # answers alone. Its top list has no code for F: F is censored (no key, no floor) and the unlisted tail is unresolved_mass.
    c_opts = list("ABCDEF")
    c_question = {"type": "choice", "name": "product_line", "instructions": "Which product line is the message about?",
                  "choices": [{"value": v, "description": d} for v, d in
                              zip(c_opts, ["Accounts and sign-in", "Payments", "Orders", "Shipping", "Returns", "Gift cards"])]}
    top = {"A": .62, "B": .21, " C": .06, "C": .03, "D": .04, "E": .02, "<eos>": .015, " ": .004}  # T-LP-1 top list
    non_option = sum(v for t, v in top.items() if t.strip() not in c_opts)
    p_lp = {o: sum(v for t, v in top.items() if t.strip() == o) / (1 - non_option) for o in c_opts if any(t.strip() == o for t in top)}
    u_lp = (1 - sum(v for t, v in top.items() if t.strip() in c_opts) - non_option) / (1 - non_option)
    c_dist = {o: r4(p) for o, p in p_lp.items()}
    assert round(sum(c_dist.values()) + r4(u_lp), 4) == 1
    c_order = sorted(c_opts, key=lambda o: (-c_dist.get(o, 0.0), c_opts.index(o)))
    n = len(c_opts) + 1  # the unresolved mass is one extra bucket
    c_conf = (p_lp[c_order[0]] - 1 / n) / (1 - 1 / n)
    c_H = -sum(x * math.log(x) for x in list(p_lp.values()) + [u_lp] if x > 0) / math.log(n)
    throttled = {"code": "throttled", "message": "Bedrock throttled the call (ThrottlingException); member calls are not retried"}
    c_members = [{"member_id": "m1", "provider": "bedrock-mantle-chat", "model": "deepseek.v3.2", "region": "us-east-1", "elicitation": "token_logprobs",
                  "input_view": "native", "status": "ok", "distribution": c_dist, "censored_options": [o for o in c_opts if o not in p_lp],
                  "unresolved_mass": r4(u_lp), "weight": 1.0, "latency_ms": LAT["m1"], "usage": usage(*CALL_USAGE["m1"], 0.00002)}]
    c_members += [{"member_id": mid, "provider": prov, "model": model, "region": "us-east-1", "elicitation": elic, "input_view": "native",
                   "status": "error", "weight": 0, "latency_ms": ms, "error": throttled} for (mid, prov, model, elic, _), ms in zip(MEMBERS[1:], (140, 95))]
    c_ans = {"type": "choice", "name": "product_line", "choice": c_order[0], "unresolved_mass": r4(u_lp),
             "candidates": [{"value": o, "probability": c_dist.get(o, 0.0), "raw_score": c_dist.get(o, 0.0), "votes": int(o == c_order[0])} for o in c_order],
             "calibration": {"status": "uncalibrated", "source": "logprob", "member_subset": ["m1"]},
             "confidence": {"kind": "heuristic", "value": r4(c_conf), "method": "normalized_max", "band": band(c_conf),
                            "margin": r4(p_lp[c_order[0]] - p_lp[c_order[1]]), "normalized_entropy": r4(c_H), "agreement": 1.0, "member_divergence": None},
             "quorum": {"required": 1, "responded": 1, "refused": 0, "total": 3}, "members": c_members}
    c_text = "I was charged but now I can't sign in to see the receipt."
    c_req = {"profile": "router-default@1", "input": c_text, "questions": [c_question], "min_quorum": 1, "include": ["members"]}
    c_res = {"id": "dec_01J9ZU4C7P", "object": "decision", "created_at": 1791504600, "status": "completed", "answers": [c_ans],
             "provenance": {"trace_id": "2c6e0a9f4b7d4e1a9c3f5b8d0e2a4c61", "profile_id": "router-default", "profile_version": "1",
                            "question_signature": sha(json.dumps([c_question], sort_keys=True, separators=(",", ":"))),
                            "input_digest": "hmac-sha256:" + hmac.new(TENANT_KEY, c_text.encode(), hashlib.sha256).hexdigest(),
                            "input_view_signature": sha(views), "option_order_seed": 1791504600,
                            "prompt_templates": {m[0]: m[4] for m in MEMBERS}, "aggregation": {"method": "linear_pool"}, "deadline_ms": 1000,
                            "quorum": {"required": 1, "responded": 1, "refused": 0, "total": 3}, "cache": {"hit": False}, "stages": [],
                            "started_at": "2026-10-09T00:08:00.000Z", "completed_at": "2026-10-09T00:08:00.512Z"},
             "usage": usage(*CALL_USAGE["m1"], 0.00002)}
    w("decision-request.valid-censored.json", c_req)
    w("decision-result.valid-censored.json", c_res)

    # OpenAI-compatible facade
    oa_req = {"model": "aghoo/router-default", "safety_identifier": "user-7f3a",
              "input": [{"role": "user", "type": "message", "content": "Export fails in Safari but works in Chrome."}],
              "questions": [{"type": "score", "instructions": "How severe is this?",
                             "levels": [{"label": "Cosmetic"}, {"label": "Workaround available"}, {"label": "Fully blocked"}]},
                            {"type": "choice", "name": "regression", "instructions": "Is this a regression?",
                             "choices": [{"value": True, "description": None}, {"value": False}]}]}
    oa_res = {"model": "aghoo/router-default@1",
              "answers": [{"type": "score", "name": None, "score": 1.1, "confidence": 0.55,
                           "probabilities": [{"value": 0, "label": "Cosmetic", "probability": 0.1},
                                             {"value": 1, "label": "Workaround available", "probability": 0.7},
                                             {"value": 2, "label": "Fully blocked", "probability": 0.2}]},
                          {"type": "choice", "name": "regression", "choice": True, "confidence": 0.44,
                           "probabilities": [{"value": True, "probability": 0.72}, {"value": False, "probability": 0.28}]}],
              "usage": {"input_tokens": 410, "input_tokens_details": {"cached_tokens": 0, "cache_write_tokens": 0},
                        "output_tokens": 132, "output_tokens_details": {"reasoning_tokens": 0}, "total_tokens": 542}}
    w("openai-request.valid.json", oa_req)
    w("openai-response.valid.json", oa_res)
    w("openai-response.invalid-unavailable-type.json", {**oa_res, "answers": [{"type": "unavailable", "name": None, "reason": "no_quorum"}]})
    w("openai-response.invalid-choice-not-declared.json", {**oa_res, "answers": [oa_res["answers"][0], {**oa_res["answers"][1], "choice": "maybe"}]})
    w("openai-request.invalid-bool-string-collision.json", {**oa_req, "questions": [oa_req["questions"][0], {**oa_req["questions"][1], "choices": [{"value": True}, {"value": "true"}, {"value": False}]}]})
    w("openai-request.invalid-256-choices.json", {**oa_req, "questions": [{"type": "choice", "instructions": "pick",
                                                                           "choices": [{"value": f"v{i}"} for i in range(256)]}]})
    pixel = "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
    w("openai-request.invalid-21-images.json", {**oa_req, "input": [{"role": "user", "content": [{"type": "input_text", "text": "Which screenshot shows the bug?"}]
                                                                    + [{"type": "input_image", "image_url": pixel}] * 21}]})
    w("openai-request.invalid-remote-image-url.json", {**oa_req, "input": [{"role": "user", "content": [
        {"type": "input_text", "text": "What does this screenshot show?"}, {"type": "input_image", "image_url": "https://example.com/shot.png"}]}]})
    oa_ens = {**oa_res, "answers": [oa_res["answers"][0], {"type": "refusal", "name": "regression"}],
              "x_aghoo": {"decision_id": "dec_01J9ZS9X2A", "calibration": {"q0": "uncalibrated"}, "mapped_unavailable": {"regression": "deadline_exceeded"}}}
    w("openai-response.valid-ensemble.json", oa_ens)  # x-aghoo-include: ensemble; regression missed the deadline and is mapped to refusal
    w("openai-response.invalid-x-aghoo-mapped-not-refusal.json", {**oa_ens, "x_aghoo": {**oa_ens["x_aghoo"], "mapped_unavailable": {"q0": "no_quorum"}}})
    # explicit name that is not a native Name: stored internally as q0; the facade echoes it and keys x_aghoo by it (IMPL-047)
    w("openai-request.valid-non-pattern-name.json", {**oa_req, "questions": [{**oa_req["questions"][0], "name": "Severity Level"}, oa_req["questions"][1]]})
    w("openai-response.valid-non-pattern-name.json", {**oa_res, "answers": [{**oa_res["answers"][0], "name": "Severity Level"}, oa_res["answers"][1]],
                                                      "x_aghoo": {"decision_id": "dec_01J9ZV5D8Q",
                                                                  "calibration": {"Severity Level": "uncalibrated", "regression": "uncalibrated"}}})
    print("examples written:", len(list(EX.glob("*.json"))))


if __name__ == "__main__":
    main()
