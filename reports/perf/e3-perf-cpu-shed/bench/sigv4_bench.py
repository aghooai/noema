"""CPU per `signed_headers` call (the lean Converse path's signing), best of 7 x 5,000 calls, one pinned core.
Run with PYTHONPATH=<tree>/src:<tree> from that tree (base snapshot vs working tree)."""
import json, sys, time, gc
from pathlib import Path
from noema.adapters._converse_wire import converse_body, converse_url, signed_headers
from noema.adapters.wire import converse_params
from tests.fakes.config import example_snapshot
from tests.golden.wire import load_wire_goldens, request_from
from tests.unit.adapters.lean_reference import creds

goldens = [g for g in load_wire_goldens() if g["op"] == "converse" and "error" not in g["expected"]]
def text_only(g):
    return all("text" in b for m in g["request"]["messages"] for b in m["content"])
dec = [g for g in goldens if text_only(g) and g["request"].get("call_kind") != "generation"]
g = dec[0] if dec else next(g for g in goldens if text_only(g))
req = request_from(g["request"])
params = converse_params(req, example_snapshot().model(req.model_alias))
body = converse_body(params, media=False)
url = converse_url("https://bedrock-runtime.us-east-1.amazonaws.com", params["modelId"])
c = creds()
gc.disable()
best = []
for _ in range(7):
    t = time.process_time_ns()
    for _ in range(5000):
        signed_headers(c, url, body)
    best.append((time.process_time_ns() - t) / 5000 / 1000)
print(json.dumps({"golden": g["name"], "body_bytes": len(body), "us_per_call_best": round(min(best), 2), "runs": [round(b, 2) for b in best]}))
