"""Children of a frame in a collapsed profile: python children.py <collapsed> <frame substring> [depth]."""
import sys
from collections import Counter
path, needle = sys.argv[1], sys.argv[2]
depth = int(sys.argv[3]) if len(sys.argv) > 3 else 1
tot = 0; c = Counter()
for line in open(path):
    stack, n = line.rsplit(" ", 1); n = int(n); tot += n
    frames = stack.split(";")
    idx = [i for i, f in enumerate(frames) if needle in f]
    if idx:
        i = idx[-1]
        child = ";".join(f.split("/")[-1] for f in frames[i + 1:i + 1 + depth]) or "<self>"
        c[child] += n
for k, v in c.most_common(25): print(f"{v/tot*100:5.2f}% {k}")
