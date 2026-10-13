"""Gen-2 collections per api process in each step window: count, interval, p50/max pause, share of wall time."""
import json, sys
from array import array
import statistics as st
run = sys.argv[1]
steps = json.load(open(f"{run}/steps.json"))
cfg = json.load(open(f"{run}/results.json"))["config"]
warm, settle = float(cfg["warmup_s"]), float(cfg["settle_s"])
for p in (0, 1):
    a = array("q"); a.frombytes(open(f"{run}/api-{p}.gc", "rb").read())
    rec = [(a[i], a[i + 1], a[i + 2]) for i in range(0, len(a), 3)]
    for i, s in enumerate(steps):
        t0 = s["t_start"] + int((warm if i == 0 else settle) * 1e9); t1 = s["t_end"]
        g2 = [d / 1e6 for t, d, g in rec if g == 2 and t0 <= t < t1]
        allp = sum(d for t, d, g in rec if t0 <= t < t1) / 1e9
        span = (t1 - t0) / 1e9
        if g2:
            print(f"api-{p} step {s['streams']}: gen2 n={len(g2)} every {span/len(g2):.1f} s p50 {st.median(g2):.0f} ms max {max(g2):.0f} ms; all GC {100*allp/span:.1f}% of wall")
