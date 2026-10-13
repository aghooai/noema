"""Validate contract examples: JSON Schema (draft 2020-12) + semantic invariants JSON Schema can't express.

Usage: python3 -I check_examples.py   (exit 0 iff every example matches its expected outcome:
*.valid*.json must pass, *.invalid-*.json must fail). Results are paired with their request (PAIRS) so
request/result invariants (names, declared values, tie order) are checked too.

Importable: the CLI runs only under `__main__`, so the server and the audit can reuse the invariant functions.
`request_invariants` and `oa_request_invariants` return `Violation`s (strings that carry the API error `param`).
"""
import hashlib
import json
import pathlib
import re
import sys
from datetime import datetime

import yaml
from jsonschema import Draft202012Validator, FormatChecker

HERE = pathlib.Path(__file__).parent
EX = HERE / "examples"
SCHEMA = json.loads((HERE / "decision.schema.json").read_text())
OA_SCHEMA = json.loads((HERE / "openai-compat.schema.json").read_text())
for s in (SCHEMA, OA_SCHEMA):
    Draft202012Validator.check_schema(s)
TOL = 1e-3
BANDS = {"high_gte": 0.75, "low_lt": 0.4}  # routing.confidence_bands in examples/platform.example.yaml
ACCEPT = {"ece_max": 0.05, "n_min": 300, "class_n_min": 30, "consensus_min": 0.6, "family_lift_max": 0.10}  # calibration acceptance (02 §5.5, 06 §2.3); family_lift_max = labeling.family_overlap_lift_max
PAIRS = {  # default: decision-request.valid.json
    "decision-result.valid-escalated.json": "decision-request.valid-image.json",
    "decision-result.invalid-image-no-adequate-view.json": "decision-request.valid-image.json",
    "decision-result.valid-censored.json": "decision-request.valid-censored.json",
    **{f"decision-result.{k}.json": "decision-request.valid-hierarchical.json"
       for k in ("valid-hierarchical", "invalid-hierarchical-unresolved-renormalized", "invalid-hierarchical-coverage", "invalid-flat-over-member-limit")},
    "decision-result.valid-hierarchical-lone-group.json": "decision-request.valid-hierarchical-lone-group.json",
}
JEV_REQUEST = "decision-request.valid-jev.json"  # decision-jev-response.* pair with this request unless listed in JEV_PAIRS
JEV_PAIRS = {"decision-jev-response.valid-k16.json": "decision-request.valid-jev-k16.json"}
TEMPLATE_MAX = {k: v["max_choices"] for k, v in yaml.safe_load((EX / "platform.example.yaml").read_text())["templates"].items()}  # per-template max_choices
OA_PAIRS = {"openai-response.valid-non-pattern-name.json": "openai-request.valid-non-pattern-name.json"}  # facade responses pair with openai-request.valid.json unless listed here
AUTO_NAME = re.compile(r"^q[0-9]+$")  # generated names for unnamed facade questions are reserved
JTOL = 0.011  # Jev values are rounded to 2 decimals
OA_MAX_IMAGES = 20  # facade: input_image parts per request (the Bedrock Converse limit; 03 §4.2)


class Violation(str):
    """An invariant error message. It is a str (printing and truthiness are unchanged) that also carries `param`,
    the path of the offending field for the API error envelope (None when the rule spans several fields)."""

    def __new__(cls, message, param=None):
        self = super().__new__(cls, message)
        self.param = param
        return self


def jev_sum_tol(k):
    """K values each rounded to 2 decimals can drift by K * 0.005 in total (never below 0.011)."""
    return max(JTOL, k * 0.005)


def validator(schema, defname):
    return Draft202012Validator({"$ref": f"#/$defs/{defname}", "$defs": schema["$defs"]}, format_checker=FormatChecker())


def ordinal_dispersion(probs_by_index):
    """TypeSafe score confidence: m = most likely level (ties -> lower index); MAD of uniform around its centre."""
    k = len(probs_by_index)
    mode = max(range(k), key=lambda i: (probs_by_index[i], -i))
    mad_u = sum(abs(i - (k - 1) / 2) for i in range(k)) / k
    return max(0.0, 1 - sum(p * abs(i - mode) for i, p in enumerate(probs_by_index)) / mad_u)


def normalized_max(probs):
    n = len(probs)
    return (max(probs) - 1 / n) / (1 - 1 / n)


def self_tests():
    # OpenAI guide examples (OA-G): choice (0.95,0.02,0.01,0.02) -> 0.93; score (0.1,0.7,0.2) -> 0.55
    assert round(normalized_max([.95, .02, .01, .02]), 2) == 0.93
    assert round(ordinal_dispersion([.1, .7, .2]), 2) == 0.55
    # mode vs median diverge: TypeSafe (mode) gives 0.25; a median-based formula would give 0.417
    assert round(ordinal_dispersion([0, .3, .3, .4, 0]), 3) == 0.25


def jev_invariants(res, req):
    errs = []
    qs = {q["name"]: q for q in req["questions"]}
    if set(res["answers"]) != set(qs):
        errs.append(f"answers {sorted(res['answers'])} != request questions {sorted(qs)}")
    for name, a in res["answers"].items():
        q = qs.get(name)
        if not q or a["type"] in ("refusal", "unavailable"):
            continue
        want = "noul" if q["type"] in ("predicate", "noul") else q["type"]
        if a["type"] != want:
            errs.append(f"{name}: Jev answer type {a['type']} != {want}")
            continue
        if a["type"] == "choice":
            vals = [c["value"] for c in q["choices"]]
            p = a["probabilities"]
            if sorted(p) != sorted(vals):
                errs.append(f"{name}: probabilities keys must be the declared choices")
            if abs(sum(p.values()) - 1) > jev_sum_tol(len(p)):
                errs.append(f"{name}: probabilities sum to {sum(p.values()):.3f}")
            top = max(vals, key=lambda v: (p.get(v, 0), -vals.index(v)))
            if a["choice"] != top:
                errs.append(f"{name}: choice {a['choice']!r} is not the argmax {top!r}")
            if abs(normalized_max([p[v] for v in vals]) - a["confidence"]) > 0.02:
                errs.append(f"{name}: confidence != normalized_max")
        if a["type"] == "score":
            n = len(q["levels"])
            keys = [str(i) for i in range(n)]
            if sorted(a["probabilities"]) != sorted(keys) or sorted(a["legend"]) != sorted(keys):  # string keys sort lexicographically ("10" < "2")
                errs.append(f"{name}: legend/probabilities keys must be 0..{n - 1}")
                continue
            pv = [a["probabilities"][k] for k in keys]
            if abs(sum(pv) - 1) > jev_sum_tol(len(pv)):
                errs.append(f"{name}: probabilities sum to {sum(pv):.3f}")
            if abs(sum(i * x for i, x in enumerate(pv)) - a["score"]) > 0.02:
                errs.append(f"{name}: score {a['score']} != expected index")
            if abs(ordinal_dispersion(pv) - a["confidence"]) > 0.02:
                errs.append(f"{name}: confidence != ordinal_dispersion")
    return errs


def member_key(value):
    """Member distributions are keyed by option value; booleans are serialized as "true" / "false"."""
    return ("true" if value else "false") if isinstance(value, bool) else value


def declared(question):
    if question["type"] == "choice":
        return [c["value"] for c in question["choices"]]
    if question["type"] == "score":
        return [lv["label"] for lv in question["levels"]]
    return [True, False]


def band_of(value, bands=None):
    b = bands or BANDS
    return "high" if value >= b["high_gte"] else "low" if value < b["low_lt"] else "medium"


def request_invariants(req):
    errs = []
    names = [q["name"] for q in req["questions"]]
    if len(names) != len(set(names)):
        errs.append(Violation("question names must be unique", "questions"))
    for i, q in enumerate(req["questions"]):
        vals = declared(q)
        field = f"questions[{i}].{'levels' if q['type'] == 'score' else 'choices'}"
        if len(vals) != len(set(map(json.dumps, vals))):
            errs.append(Violation(f"{q['name']}: choice values / level labels must be unique", field))
        if q["type"] == "choice":
            groups = [c.get("group") for c in q["choices"]]
            if any(groups) and not all(groups):
                errs.append(Violation(f"{q['name']}: either every choice has a group or none does", field))
            if all(groups) and len(set(groups)) < 2:
                errs.append(Violation(f"{q['name']}: groups need at least two distinct labels", field))
    return errs


def question_signature(question):
    """sha256 of one question's canonical definition (the calibrator binding uses this, not the whole set)."""
    return "sha256:" + hashlib.sha256(json.dumps(question, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def answering_stage(result, name):
    """The last stage that answered this question (the last escalation listing it, else provenance):
    (profile_id, profile_version, input_view_signature, prompt_templates)."""
    p = result["provenance"]
    for stage in reversed(p["stages"]):
        if name in stage["questions"]:
            return (stage["profile_id"], stage["profile_version"],
                    stage.get("input_view_signature", p["input_view_signature"]), stage.get("prompt_templates", p["prompt_templates"]))
    return p["profile_id"], p["profile_version"], p["input_view_signature"], p["prompt_templates"]


def holdout_errors(h, classes, *, accept=None):
    """Holdout acceptance (06 §2.3) as far as the wire artifact can show it."""
    accept = accept or ACCEPT
    errs = []
    if h["ece"] > accept["ece_max"] or h["n"] < accept["n_min"]:
        errs.append(f"calibrated but holdout fails acceptance (ece {h['ece']}, n {h['n']})")
    if h["nll"] > h["nll_uncalibrated"]:
        errs.append(f"calibrated NLL {h['nll']} is worse than uncalibrated {h['nll_uncalibrated']}")
    if set(h["consensus_rate_by_class"]) != set(classes) or set(h["n_by_class"]) != set(classes):
        errs.append(f"holdout classes must be exactly the declared values {sorted(classes)}")
    else:
        low = {c: r for c, r in h["consensus_rate_by_class"].items() if r < accept["consensus_min"]}
        if low or h["consensus_rate"] < accept["consensus_min"]:
            errs.append(f"consensus rate below {accept['consensus_min']} (overall {h['consensus_rate']}, classes {low}); a zero rate is no evidence")
        if sum(h["n_by_class"].values()) != h["n"]:
            errs.append(f"n_by_class sums to {sum(h['n_by_class'].values())}, not n = {h['n']}")
        if sorted(h["flagged_classes"]) != sorted(c for c, k in h["n_by_class"].items() if k < accept["class_n_min"]):
            errs.append(f"flagged_classes must be exactly the classes with n < {accept['class_n_min']}")
    fo = h["family_overlap"]
    if fo["threshold"] > accept["family_lift_max"] or fo["max_member_lift"] > fo["threshold"]:
        errs.append(f"labeler-family overlap lift {fo['max_member_lift']} exceeds its threshold {fo['threshold']} (max {accept['family_lift_max']})")
    return errs


def template_limit_errors(a, k, result, method, *, template_max=None):
    """A member only answers a question its template can carry: flat needs K <= max_choices; hierarchical needs every group
    count and group size <= max_choices. Members that cannot are skipped with reason too_many_choices (never answered)."""
    template_max = template_max or TEMPLATE_MAX
    errs = []
    need = k if method == "flat" else max([len(a["groups"])] + [len(g["options"]) for g in a["groups"]])
    limits = []
    for m in a.get("members", []):
        tpl = answering_stage(result, a["name"])[3].get(m["member_id"])
        limit = template_max.get(tpl)
        if limit is None:
            continue
        limits.append(limit)
        if m["status"] == "ok" and limit < need:
            errs.append(f"member {m['member_id']} ({tpl}, max_choices {limit}) answered a {method} question that needs {need}; it must be skipped with too_many_choices")
        if m["status"] == "skipped" and m.get("skip_reason") == "too_many_choices" and limit >= need:
            errs.append(f"member {m['member_id']} was skipped with too_many_choices although {tpl} carries {limit} >= {need}")
    if method == "hierarchical" and limits and min(limits) >= k:
        errs.append(f"hierarchical method used although every member carries {k} options flat")
    return errs


def hierarchy_errors(a, q, result):
    """Hierarchical answer (08 §3.4): groups partition the options, follow the request's grouping, and the expanded ones are the
    top groups by stage-1 mass until they cover >= 0.9 and hold >= 2 options (at most 3 groups). Masses must add up."""
    errs, groups, dv = [], a["groups"], declared(q)
    options = [o for g in groups for o in g["options"]]
    if sorted(options) != sorted(dv):
        return ["groups must partition the declared options exactly once"]
    supplied = {c["value"]: c.get("group") for c in q["choices"]}
    if all(supplied.values()):
        errs += [f"option {o!r} is in group {g['label']!r} but the request put it in {supplied[o]!r}" for g in groups for o in g["options"] if supplied[o] != g["label"]]
    else:  # server split: contiguous declared-order ranges, balanced, labeled by 1-based index range
        pos = 0
        for g in groups:
            idx = [dv.index(o) for o in g["options"]]
            if idx != list(range(pos, pos + len(idx))) or g["label"] != f"options {pos + 1}-{pos + len(idx)}":
                errs.append(f"server-split group {g['id']} must be the contiguous range labeled 'options {pos + 1}-{pos + len(idx)}'")
            pos += len(idx)
        sizes = [len(g["options"]) for g in groups]
        if max(sizes) - min(sizes) > 1:
            errs.append("server-split groups must be balanced (sizes differ by at most 1)")
    if abs(sum(g["probability"] for g in groups) - 1) > TOL:
        errs.append(f"stage-1 group probabilities sum to {sum(g['probability'] for g in groups):.4f}")
    if abs(a.get("unresolved_mass", 0) - sum(g["probability"] for g in groups if not g["expanded"])) > TOL:
        errs.append("unresolved_mass must equal the mass of the unexpanded groups")
    by_value = {c["value"]: c["probability"] for c in a["candidates"]}
    for g in groups:
        if g["expanded"] and all(o in by_value for o in g["options"]) and abs(sum(by_value[o] for o in g["options"]) - g["probability"]) > 2 * TOL:
            errs.append(f"candidates of group {g['id']} sum to {sum(by_value[o] for o in g['options']):.4f}, not the group's stage-1 mass {g['probability']}")
    units = [round(g["probability"] * 10_000) for g in groups]   # emitted 4-dp masses as integers (server rule, spec-06 §19.6)
    order = sorted(range(len(groups)), key=lambda i: (-units[i], i))
    need, covered, n_options = 0, 0, 0
    for i in order[:3]:
        need += 1
        covered += units[i]
        n_options += len(groups[i]["options"])
        if covered >= 9_000 and n_options >= 2:  # a lone one-option group is never a scored answer (>= 2 candidates)
            break
    if {i for i, g in enumerate(groups) if g["expanded"]} != set(order[:need]):
        errs.append(f"expanded groups must be the top {need} by stage-1 mass (cover >= 0.9 and hold >= 2 options, at most 3)")
    return errs


ADEQUATE_IMAGE_VIEWS = {"native"}  # 02 §5.3: ocr_text and thumbnail are degraded views of an image


def has_image(req):
    parts = req["input"] if isinstance(req["input"], list) else []
    return any(p.get("type") == "input_image" for m in parts for p in (m["content"] if isinstance(m["content"], list) else []))


def result_invariants(result, req, *, bands=None, accept=None, template_max=None):
    """bands / accept / template_max default to the module constants (design examples); the server's test-mode validator passes
    the active config snapshot's values (IMPL-039)."""
    errs = []
    qs = {q["name"]: q for q in req["questions"]}
    names = [a["name"] for a in result["answers"]]
    if len(names) != len(set(names)):
        errs.append("answer names must be unique")
    if set(names) != set(qs):
        errs.append(f"answers {sorted(names)} != request questions {sorted(qs)}")
    max_member_ms = 0
    for a in result["answers"]:
        tag, q = a["name"], qs.get(a["name"])
        if q and a["type"] in ("predicate", "choice", "score") and a["type"] != q["type"]:
            errs.append(f"{tag}: answer type {a['type']} != question type {q['type']}")
        for m in a.get("members", []):
            if m["status"] == "ok":
                max_member_ms = max(max_member_ms, m["latency_ms"])
        if "candidates" not in a:
            continue
        cands = a["candidates"]
        probs = [c["probability"] for c in cands]
        unresolved = a.get("unresolved_mass", 0)
        method = a.get("choice_method", "flat")
        if abs(sum(probs) + unresolved - 1) > TOL:
            errs.append(f"{tag}: candidate probabilities {sum(probs):.4f} + unresolved_mass {unresolved} must sum to 1 (a censored or unexpanded tail is never renormalized away)")
        if (method == "hierarchical") != ("groups" in a):
            errs.append(f"{tag}: groups are present exactly when choice_method is hierarchical")
        if q:
            dv = declared(q)
            covered = dv if method == "flat" else [o for g in a.get("groups", []) if g["expanded"] for o in g["options"]]
            if sorted(map(json.dumps, (c["value"] for c in cands))) != sorted(map(json.dumps, covered)):
                errs.append(f"{tag}: candidates must cover exactly the declared values" + ("" if method == "flat" else " of the expanded groups"))
            if method == "hierarchical" and a["type"] == "choice" and "groups" in a:
                errs += [f"{tag}: {e}" for e in hierarchy_errors(a, q, result)]
            if a["type"] == "choice":
                errs += [f"{tag}: {e}" for e in template_limit_errors(a, len(dv), result, method, template_max=template_max)]
            order = sorted(cands, key=lambda c: (-round(c["probability"], 4), dv.index(c["value"]) if c["value"] in dv else 0))
            if [c["value"] for c in order] != [c["value"] for c in cands]:
                errs.append(f"{tag}: candidates not sorted by probability with declared-order ties")
        if a["type"] == "choice" and a["choice"] != cands[0]["value"]:
            errs.append(f"{tag}: choice {a['choice']!r} is not the argmax {cands[0]['value']!r}")
        if a["type"] == "predicate":
            p_true = next((c["probability"] for c in cands if c["value"] is True), None)
            if p_true is None or abs(p_true - a["probability"]) > TOL:
                errs.append(f"{tag}: probability != P(true) candidate")
        conf = a["confidence"]
        expected_method = "ordinal_dispersion" if a["type"] == "score" else "normalized_max"
        if conf["method"] in ("normalized_max", "ordinal_dispersion") and conf["method"] != expected_method:
            errs.append(f"{tag}: confidence method {conf['method']} is wrong for a {a['type']} answer")
        if a["type"] == "score":
            if q and any(c["value"] in declared(q) and c["index"] != declared(q).index(c["value"]) for c in cands):
                errs.append(f"{tag}: candidate index must equal the label's position in the requested levels")
            expected = sum(c["index"] * c["probability"] for c in cands)
            if abs(expected - a["score"]) > TOL:
                errs.append(f"{tag}: score {a['score']} != expected index {expected:.4f}")
            if a["level"] != cands[0]["value"]:
                errs.append(f"{tag}: level is not the most probable label")
            if sorted(c["index"] for c in cands) != list(range(len(cands))):
                errs.append(f"{tag}: level indices must be 0..n-1")
            want = ordinal_dispersion([c["probability"] for c in sorted(cands, key=lambda c: c["index"])])
        else:
            # the unresolved mass is one extra bucket (n = candidates + 1) that is never the peak: p_max is the top candidate
            want = (max(probs) - 1 / (len(probs) + 1)) / (1 - 1 / (len(probs) + 1)) if unresolved else normalized_max(probs)
        if conf["method"] == expected_method and abs(want - conf["value"]) > 2 * TOL:
            errs.append(f"{tag}: confidence {conf['value']} != {conf['method']} formula {want:.4f}")
        cal = a["calibration"]
        band_basis = max(probs) if cal["status"] == "calibrated" else conf["value"]
        if conf["band"] != band_of(band_basis, bands):
            errs.append(f"{tag}: band {conf['band']} inconsistent with {band_basis:.3f} and bands {bands or BANDS}")
        if cal["status"] == "calibrated":
            errs += [f"{tag}: {e}" for e in holdout_errors(cal["holdout"], [member_key(c["value"]) for c in cands], accept=accept)]
            b, stage = cal["binding"], answering_stage(result, a["name"])
            if (b["profile_id"], b["profile_version"]) != stage[:2]:
                errs.append(f"{tag}: calibrator fitted for {b['profile_id']}@{b['profile_version']} but the answer came from {stage[0]}@{stage[1]}; serve uncalibrated")
            if q and b["question_signature"] != question_signature(q):
                errs.append(f"{tag}: calibration binding question_signature does not match the requested question")
            if b["input_view_signature"] != stage[2]:
                errs.append(f"{tag}: calibration binding input_view_signature != the views this decision used")
            if b["choice_method"] != a.get("choice_method", "flat"):
                errs.append(f"{tag}: calibration binding choice_method {b['choice_method']} != the answer's method")
        members = a.get("members", [])
        ok = [m for m in members if m["status"] == "ok"]
        cand_keys = {member_key(c["value"]) for c in cands}
        keys_ok = True
        for m in ok:
            want_keys = cand_keys - set(m.get("censored_options", []))  # censored options carry no probability: no floor is invented
            if set(m["distribution"]) != want_keys:
                keys_ok = False
                errs.append(f"{tag}/{m['member_id']}: distribution keys {sorted(m['distribution'])} are not the declared options {sorted(want_keys)}")
            elif abs(sum(m["distribution"].values()) + m.get("unresolved_mass", 0) - 1) > TOL:
                errs.append(f"{tag}/{m['member_id']}: member distribution {sum(m['distribution'].values()):.4f} + unresolved_mass {m.get('unresolved_mass', 0)} must sum to 1")
        if ok and has_image(req) and not any(m["input_view"] in ADEQUATE_IMAGE_VIEWS for m in ok):
            errs.append(f"{tag}: the request has an image but no answering member has an adequate (native) view; the answer is unavailable / no_capable_member")
        if members:
            qi = a["quorum"]
            if qi["responded"] != len(ok) or qi["total"] != len(members):
                errs.append(f"{tag}: quorum {qi} disagrees with members ({len(ok)} ok of {len(members)})")
            if qi["refused"] != sum(1 for m in members if m["status"] == "refused"):
                errs.append(f"{tag}: quorum.refused {qi['refused']} != members with status refused")
            if qi["responded"] + qi["refused"] > qi["total"]:
                errs.append(f"{tag}: responded + refused exceeds total")
            if qi["responded"] < qi["required"]:
                errs.append(f"{tag}: answered below its quorum")
            if cal.get("member_subset") and sorted(cal["member_subset"]) != sorted(m["member_id"] for m in ok):
                errs.append(f"{tag}: calibration.member_subset != answering members")
            if result["provenance"]["aggregation"]["method"] == "linear_pool" and ok and keys_ok:
                wsum = sum(m["weight"] for m in ok)
                for c in cands:
                    key = json.dumps(c["value"]) if isinstance(c["value"], bool) else c["value"]
                    key = key if not isinstance(c["value"], bool) else ("true" if c["value"] else "false")
                    pooled = sum(m["weight"] * (m.get("calibrated_distribution") or m["distribution"]).get(key, 0.0) for m in ok) / wsum
                    if abs(pooled - c["raw_score"]) > 2 * TOL:
                        errs.append(f"{tag}: raw_score {c['raw_score']} for {c['value']!r} != linear pool {pooled:.4f}")
                        break
        if cal["status"] == "uncalibrated" and any(abs(c["probability"] - c["raw_score"]) > 2 * TOL for c in cands):
            errs.append(f"{tag}: uncalibrated probabilities must equal raw scores")
    usages = [("usage", result["usage"])] + [(f"stages[{i}].usage", st["usage"]) for i, st in enumerate(result["provenance"]["stages"])]
    usages += [(f"{a['name']}/{m['member_id']}.usage", m["usage"]) for a in result["answers"] for m in a.get("members", []) if "usage" in m]
    for where, u in usages:
        if u["cached_input_tokens"] + u["cache_write_input_tokens"] > u["input_tokens"]:
            errs.append(f"{where}: cache reads + writes exceed input_tokens (they are parts of it)")
        if u["reasoning_tokens"] > u["output_tokens"]:
            errs.append(f"{where}: reasoning_tokens exceed output_tokens (they are a part of it)")
    p = result["provenance"]
    q = p["quorum"]
    if not q["required"] <= q["total"] or q["responded"] > q["total"]:
        errs.append("quorum counts inconsistent")
    t0 = datetime.fromisoformat(p["started_at"].replace("Z", "+00:00"))
    t1 = datetime.fromisoformat(p["completed_at"].replace("Z", "+00:00"))
    if (t1 - t0).total_seconds() * 1000 + 1 < max_member_ms:
        errs.append(f"decision took {(t1 - t0).total_seconds() * 1000:.0f} ms but a member reports {max_member_ms} ms")
    if p["stages"] and "stages" not in req.get("include", []):
        pass  # stages are always recorded; include only controls the response view
    return errs


def oa_request_invariants(req):
    """Facade request rules JSON Schema can't express: name uniqueness, reserved q{i} names, collision-free choice identity,
    at most OA_MAX_IMAGES images."""
    errs = []
    named = [q["name"] for q in req["questions"] if q.get("name") is not None]
    if len(named) != len(set(named)):
        errs.append(Violation("explicit question names must be unique", "questions"))
    errs += [Violation(f"name {q['name']!r} is reserved for generated q{{i}} names", f"questions[{i}].name")
             for i, q in enumerate(req["questions"]) if q.get("name") is not None and AUTO_NAME.match(q["name"])]
    for i, q in enumerate(req["questions"]):
        if q["type"] == "choice":
            keys = [member_key(c["value"]) for c in q["choices"]]  # true and "true" serialize identically for members
            if len(keys) != len(set(keys)):
                errs.append(Violation(f"questions[{i}]: choice values collide after serialization (true/\"true\", false/\"false\" or duplicates)",
                                      f"questions[{i}].choices"))
        if q["type"] == "score" and len({lv["label"] for lv in q["levels"]}) != len(q["levels"]):
            errs.append(Violation(f"questions[{i}]: level labels must be unique", f"questions[{i}].levels"))
    messages = req["input"] if isinstance(req["input"], list) else []
    images = sum(p.get("type") == "input_image" for m in messages if isinstance(m["content"], list) for p in m["content"])
    if images > OA_MAX_IMAGES:
        errs.append(Violation(f"{images} input_image parts; at most {OA_MAX_IMAGES} per request (Bedrock limit)", "input"))
    return errs


def oa_invariants(res, req):
    """Facade response paired with its request: order/name echo, declared values, normalization, argmax, formulas, usage."""
    errs = []
    if len(res["answers"]) != len(req["questions"]):
        return [f"{len(res['answers'])} answers for {len(req['questions'])} questions"]
    for i, (a, q) in enumerate(zip(res["answers"], req["questions"])):
        tag = q.get("name") or f"q{i}"
        if a["type"] == "refusal":
            continue
        if a["type"] != q["type"]:
            errs.append(f"{tag}: answer type {a['type']} != question type {q['type']}")
            continue
        if a["name"] != q.get("name"):
            errs.append(f"{tag}: name {a['name']!r} must echo the request ({q.get('name')!r})")
        if a["type"] == "choice":
            values = [c["value"] for c in q["choices"]]
            got = [p["value"] for p in a["probabilities"]]
            if json.dumps(got) != json.dumps(values):
                errs.append(f"{tag}: probabilities must list the declared choices in declared order")
                continue
            probs = [p["probability"] for p in a["probabilities"]]
            if abs(sum(probs) - 1) > TOL:
                errs.append(f"{tag}: probabilities sum to {sum(probs):.4f}")
            top = max(range(len(values)), key=lambda j: (probs[j], -j))
            if json.dumps(a["choice"]) != json.dumps(values[top]):
                errs.append(f"{tag}: choice {a['choice']!r} is not the argmax {values[top]!r}")
            if abs(normalized_max(probs) - a["confidence"]) > 0.02:
                errs.append(f"{tag}: confidence != normalized_max")
        if a["type"] == "score":
            labels = [lv["label"] for lv in q["levels"]]
            if [p["value"] for p in a["probabilities"]] != list(range(len(labels))) or [p["label"] for p in a["probabilities"]] != labels:
                errs.append(f"{tag}: probabilities must list every level, in index order, with the requested labels")
                continue
            probs = [p["probability"] for p in a["probabilities"]]
            if abs(sum(probs) - 1) > TOL:
                errs.append(f"{tag}: probabilities sum to {sum(probs):.4f}")
            if abs(sum(j * x for j, x in enumerate(probs)) - a["score"]) > 0.02:
                errs.append(f"{tag}: score {a['score']} != expected index")
            if abs(ordinal_dispersion(probs) - a["confidence"]) > 0.02:
                errs.append(f"{tag}: confidence != ordinal_dispersion")
    x = res.get("x_aghoo")  # present only with x-aghoo-include: ensemble; keyed by effective names (given name or q{i})
    if x is not None:
        effective = [q.get("name") or f"q{i}" for i, q in enumerate(req["questions"])]
        refused = {n for n, a in zip(effective, res["answers"]) if a["type"] == "refusal"}
        if set(x["calibration"]) != set(effective) - refused:
            errs.append(f"x_aghoo.calibration keys {sorted(x['calibration'])} != non-refusal answers {sorted(set(effective) - refused)}")
        if not set(x.get("mapped_unavailable", {})) <= refused:
            errs.append(f"x_aghoo.mapped_unavailable names answers that are not refusals: {sorted(set(x['mapped_unavailable']) - refused)}")
    u = res["usage"]
    if u["total_tokens"] != u["input_tokens"] + u["output_tokens"]:
        errs.append(f"usage.total_tokens {u['total_tokens']} != input + output")
    d = u["input_tokens_details"]
    if d["cached_tokens"] + d["cache_write_tokens"] > u["input_tokens"]:
        errs.append("usage: cached + cache-write tokens exceed input_tokens")
    if u["output_tokens_details"]["reasoning_tokens"] > u["output_tokens"]:
        errs.append("usage: reasoning_tokens exceed output_tokens")
    return errs


def check(path):
    doc = json.loads(path.read_text())
    n = path.name
    if n.startswith("decision-jev-response"):
        errs = [f"schema: {e.json_path}: {e.message[:120]}" for e in validator(SCHEMA, "JevDecisionResponse").iter_errors(doc)]
        return errs or jev_invariants(doc, json.loads((EX / JEV_PAIRS.get(n, JEV_REQUEST)).read_text()))
    if n.startswith("openai-"):
        is_request = n.startswith("openai-request")
        errs = [f"schema: {e.json_path}: {e.message[:120]}" for e in validator(OA_SCHEMA, "OADecisionCreateParams" if is_request else "OADecisionResponse").iter_errors(doc)]
        if errs:
            return errs
        if is_request:
            return oa_request_invariants(doc)
        return oa_invariants(doc, json.loads((EX / OA_PAIRS.get(n, "openai-request.valid.json")).read_text()))
    defname = "DecisionCreateParams" if n.startswith("decision-request") else "DecisionResult"
    errs = [f"schema: {e.json_path}: {e.message[:140]}" for e in validator(SCHEMA, defname).iter_errors(doc)]
    if errs:
        return errs
    if defname == "DecisionCreateParams":
        return request_invariants(doc)
    req = json.loads((EX / PAIRS.get(n, "decision-request.valid.json")).read_text())
    return result_invariants(doc, req)


def main():
    self_tests()
    failures = 0
    for path in sorted(EX.glob("*.json")):
        errs = check(path)
        expect_valid = ".valid" in path.name
        ok = (not errs) == expect_valid
        failures += not ok
        detail = "valid" if not errs else f"rejected: {errs[0]}"
        print(f"{'PASS' if ok else 'FAIL'}  {path.name:62s} {detail[:120]}")
    print(f"\nconfidence self-tests passed; {failures} unexpected outcome(s)")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
