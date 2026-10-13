"""Per variant and stream count: the two ABBA runs side by side and their mean; deltas vs the previous variant."""
import json
runs = {"base": ["c1-base", "c8-base"], "a1": ["c2-a1", "c7-a1"], "a2": ["c3-a2", "c6-a2"], "a3": ["c4-a3", "c5-a3"]}
def get(name):
    return json.load(open(f"/tmp/perfa/results/{name}.json"))["steps"]
rows = {}
for v, names in runs.items():
    for name in names:
        try:
            steps = get(name)
        except FileNotFoundError:
            continue
        for s in steps:
            r = s["resources"]
            m = rows.setdefault((v, s["streams"]), {"cpu": [], "p50": [], "p95": [], "p99": [], "e2e50": [], "ingress50": [], "dec50": [], "lag99": [], "valid": []})
            m["cpu"].append(r["api_cpu_cores"] * 1000 / s["turns_per_s"])
            m["p50"].append(s["ttft_overhead_ms"]["p50"][0]); m["p95"].append(s["ttft_overhead_ms"]["p95"][0]); m["p99"].append(s["ttft_overhead_ms"]["p99"][0])
            m["e2e50"].append(s["e2e_overhead_ms"]["p50"][0]); m["ingress50"].append(s["ingress_ms"]["p50"])
            m["dec50"].append(s["decision_ms"]["p50"][0]); m["lag99"].append(s["lag_ms"]["api"]["p99"]); m["valid"].append(s["validity"]["valid"])
mean = lambda xs: sum(xs) / len(xs)
print("| variant | streams | CPU ms/turn (runs) | mean | Δ vs previous | TTFT ovh p50 (runs) | p95 (runs) | p99 mean | e2e ovh p50 mean | ingress p50 mean | decision_ms p50 mean | api lag p99 mean | valid |")
print("|---|---|---|---|---|---|---|---|---|---|---|---|---|")
prev = {}
for (v, streams), m in rows.items():
    c = mean(m["cpu"]); d = "" if streams not in prev else f"{c - prev[streams]:+.1f}"
    prev[streams] = c
    print(f"| {v} | {streams} | {' / '.join(f'{x:.1f}' for x in m['cpu'])} | {c:.1f} | {d} | {' / '.join(f'{x:.1f}' for x in m['p50'])} | {' / '.join(f'{x:.1f}' for x in m['p95'])} | {mean(m['p99']):.1f} | {mean(m['e2e50']):.1f} | {mean(m['ingress50']):.1f} | {mean(m['dec50']):.0f} | {mean(m['lag99']):.1f} | {all(m['valid'])} |")
