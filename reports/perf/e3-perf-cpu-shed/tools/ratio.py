"""Client-TTFT p95 per window with bootstrap 95% CIs and the CI of the p95 ratio (independent resamples)."""
import json, sys
from pathlib import Path
import numpy as np
sys.path.insert(0, "/codes/aghooai/noema")
from tests.load import stats

def windows(run: Path):
    steps = json.loads((run / "steps.json").read_text())
    cfg = json.loads((run / "results.json").read_text())["config"]
    warm, settle = float(cfg["warmup_s"]), float(cfg["settle_s"])
    turns = [t for t in stats.read_jsonl(sorted(run.glob("driver-*.jsonl"))) if t.get("group", "main") == "main"]
    mock = stats.read_jsonl(sorted(run.glob("mock-*.jsonl")))
    out = []
    for i, s in enumerate(steps):
        t0 = s["t_start"] + int((warm if i == 0 else settle) * 1e9)
        joined, _ = stats.join(stats.window(turns, t0, s["t_end"]), mock)
        out.append(np.array([j.ttft_ms for j in joined]))
    return out

def ratio_ci(a, b, q=95, n=1000, seed=7):
    rng = np.random.default_rng(seed)
    r = [np.percentile(b[rng.integers(0, b.size, b.size)], q) / np.percentile(a[rng.integers(0, a.size, a.size)], q) for _ in range(n)]
    return float(np.percentile(b, q) / np.percentile(a, q)), *map(float, np.percentile(r, [2.5, 97.5]))

run = Path(sys.argv[1]); w = windows(run)
for i, x in enumerate(w):
    print(i, x.size, "p95", [round(v, 1) for v in stats.percentile_ci(list(x), 95)])
for a, b in [tuple(map(int, p.split(":"))) for p in sys.argv[2:]]:
    print(f"ratio p95 step{b}/step{a}", [round(v, 4) for v in ratio_ci(w[a], w[b])])
