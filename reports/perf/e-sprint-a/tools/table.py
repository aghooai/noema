"""One markdown row per step of results JSON files: python table.py name=path ..."""
import json, sys
def p(d, k):
    v = d.get(k)
    return v
def ci(s, key):
    v = s[key]
    if isinstance(v, dict):
        return v
    return {}
print("| run | streams | n | turns/s | TTFT ovh p50 [CI] | p95 [CI] | p99 | e2e ovh p50 | p95 | ingress p50 | decision_ms p50/p95 | api lag p99 | api CPU (cores) | CPU ms/turn | load mean/max | valid | errors |")
print("|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|")
for arg in sys.argv[1:]:
    name, path = arg.split("=", 1)
    d = json.load(open(path))
    for s in d["steps"]:
        t = s["ttft_overhead_ms"]; e = s["e2e_overhead_ms"]; r = s["resources"]
        def f(x): return f"{x:.1f}"
        def pc(m, q):
            v = m[q]
            if isinstance(v, (list, tuple)): return f"{v[0]:.1f} [{v[1]:.1f}, {v[2]:.1f}]"
            if isinstance(v, dict): return f"{v['value']:.1f} [{v['low']:.1f}, {v['high']:.1f}]" if 'value' in v else str(v)
            return f(v)
        dm = s.get("decision_ms") or {}
        lag = s.get("lag_ms", {}).get("api", {})
        cpu = r["api_cpu_cores"]; tps = s["turns_per_s"]
        la = r["loadavg_1m"]
        print(f"| {name} | {s['streams']} | {s['turns_timed']} | {tps:.2f} | {pc(t,'p50')} | {pc(t,'p95')} | {pc(t,'p99')} | {pc(e,'p50')} | {pc(e,'p95')} | {pc(s['ingress_ms'],'p50')} | {pc(dm,'p50')}/{pc(dm,'p95')} | {lag.get('p99', float('nan')):.1f} | {cpu:.3f} | {cpu*1000/tps:.1f} | {la['mean']:.1f}/{la['max']:.1f} | {'yes' if s['validity']['valid'] else 'no'} | {s['errors'] or 0} |")
