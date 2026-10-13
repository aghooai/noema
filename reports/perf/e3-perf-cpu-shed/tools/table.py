"""Markdown rows per run and step: n, turns/s, TTFT overhead p50/p95/p99 [CI], e2e overhead p50/p95, ingress p50,
api lag p99, pod CPU (cores), CPU ms/turn, load mean, valid, errors. argv: results json files."""
import json, sys
from pathlib import Path
def ci(x): return f"{x[0]:.2f} [{x[1]:.2f}, {x[2]:.2f}]" if isinstance(x, list) else f"{x:.2f}"
for f in sys.argv[1:]:
    r = json.loads(Path(f).read_text()); name = Path(f).stem
    for s in r["steps"]:
        t, e = s["ttft_overhead_ms"], s["e2e_overhead_ms"]
        cores, tps = s["resources"]["api_cpu_cores"], s["turns_per_s"]
        p99 = t["p99"][0] if isinstance(t["p99"], list) else t["p99"]
        print(f"| {name} | {s['streams']} | {t['n']:,} | {tps:.2f} | {ci(t['p50'])} | {ci(t['p95'])} | {p99:.1f} | "
              f"{e['p50'][0]:.1f} / {e['p95'][0]:.1f} | {s['ingress_ms']['p50']:.1f} | {s['lag_ms']['api']['p99']:.1f} | "
              f"{cores:.3f} | {1000 * cores / tps:.1f} | {s['resources']['loadavg_1m']['mean']:.1f} | "
              f"{'yes' if s['validity']['valid'] else 'no'} | {s['errors'] or 0} |")
