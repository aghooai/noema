"""C5 rows: victim client TTFT p95 solo -> flood, ratio [95% CI] (ratio.py's bootstrap), victim errors, victim TTFT
overhead / ingress / decision p95 under the flood, pod CPU solo -> flood, full-GC p50 per process under the flood,
the flooder's statuses, admission queue_timeouts and cap/edge sheds (both processes), load, validity."""
import json, statistics as st, sys
from array import array
from pathlib import Path
import numpy as np
sys.path.insert(0, "/codes/aghooai/noema")
from tests.load import stats

def windows(run):
    steps = json.loads((run / "steps.json").read_text()); cfg = json.loads((run / "results.json").read_text())["config"]
    warm, settle = float(cfg["warmup_s"]), float(cfg["settle_s"])
    turns = [t for t in stats.read_jsonl(sorted(run.glob("driver-*.jsonl"))) if t.get("group", "main") == "main"]
    mock = stats.read_jsonl(sorted(run.glob("mock-*.jsonl")))
    out = []
    for i, s in enumerate(steps):
        t0 = s["t_start"] + int((warm if i == 0 else settle) * 1e9)
        joined, _ = stats.join(stats.window(turns, t0, s["t_end"]), mock)
        out.append((np.array([j.ttft_ms for j in joined]), t0, s["t_end"]))
    return out

def gc_p50(run, p, t0, t1):
    a = array("q"); a.frombytes((run / f"api-{p}.gc").read_bytes())
    g2 = [a[i + 1] / 1e6 for i in range(0, len(a), 3) if a[i + 2] == 2 and t0 <= a[i] < t1]
    return st.median(g2) if g2 else float("nan")

def metric(run, name):
    tot = 0.0
    for f in run.glob("metrics-step1*.txt"):
        for line in f.read_text().splitlines():
            if line.startswith(name):
                tot += float(line.rsplit(" ", 1)[1])
    return int(tot)

for arg in sys.argv[1:]:
    run = Path(arg); r = json.loads((run / "results.json").read_text()); s0, s1 = r["steps"]
    (a, _, _), (b, t0, t1) = windows(run)
    rng = np.random.default_rng(7)
    rs = [np.percentile(b[rng.integers(0, b.size, b.size)], 95) / np.percentile(a[rng.integers(0, a.size, a.size)], 95) for _ in range(1000)]
    lo, hi = np.percentile(rs, [2.5, 97.5]); ratio = np.percentile(b, 95) / np.percentile(a, 95)
    fl = s1.get("side", {}).get("flood", {}).get("status", {})
    print(f"| {run.name} | {np.percentile(a, 95):.1f} -> {np.percentile(b, 95):.1f} | **{ratio:.3f}** [{lo:.3f}, {hi:.3f}] | "
          f"{sum(s1['errors'].values()) if s1['errors'] else 0} | {s1['ttft_overhead_ms']['p95'][0]:.1f} | {s1['ingress_ms']['p95']:.1f} | "
          f"{s1['decision_ms']['p95'][0]:.0f} | {s0['resources']['api_cpu_cores']:.2f} -> {s1['resources']['api_cpu_cores']:.2f} | "
          f"{gc_p50(run, 0, t0, t1):.0f} / {gc_p50(run, 1, t0, t1):.0f} | {fl.get('200', 0):,} / {fl.get('429', 0):,} / {fl.get('None', 0)} | "
          f"{metric(run, 'aghoo_admission_rejections_total{code=\"queue_timeout\"}'):,} / {metric(run, 'aghoo_turns_shed_total'):,} | "
          f"{s0['resources']['loadavg_1m']['mean']:.1f} / {s1['resources']['loadavg_1m']['mean']:.1f} | "
          f"{'yes' if s0['validity']['valid'] and s1['validity']['valid'] else 'no'} |")
