"""Inclusive share of samples whose stack contains each pattern (both api processes); argv: run dir, patterns..."""
import sys
from pathlib import Path
run = Path(sys.argv[1]); pats = sys.argv[2:]
total = 0; hits = {p: 0 for p in pats}
for f in sorted(run.glob("profile-cpu-*.collapsed")):
    for line in f.read_text().splitlines():
        stack, _, n = line.rpartition(" ")
        n = int(n); total += n
        for p in pats:
            if all(part in stack for part in p.split("&")) and not any(x in stack for x in p.split("!")[1:] if "!" in p):
                hits[p] += n
for p in pats:
    print(f"{100 * hits[p] / total:6.2f}%  {p}")
print("total samples", total)
