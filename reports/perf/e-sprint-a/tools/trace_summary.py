"""Median timeline of createTurn traces: per label, median start and duration (ms from the ingress entry)."""
import json, statistics, sys
from collections import defaultdict
rows = [json.loads(l) for l in open(sys.argv[1])]
rows = rows[2:] if len(rows) > 4 else rows  # drop the first (cold) turns
starts, durs, counts = defaultdict(list), defaultdict(list), defaultdict(list)
for r in rows:
    seen = defaultdict(int)
    for name, start, dur in r["events"]:
        seen[name] += 1
        if seen[name] == 1:
            starts[name].append(start); durs[name].append(dur)
    for name, n in seen.items():
        counts[name].append(n)
print(f"{len(rows)} traces")
for name in sorted(starts, key=lambda n: statistics.median(starts[n])):
    print(f"{name:34s} start {statistics.median(starts[name]):8.2f}  dur {statistics.median(durs[name]):7.2f}  count {statistics.median(counts[name]):5.0f}")
