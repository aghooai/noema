"""Inclusive share and ms of CPU per turn of the changed paths, base vs e3b profiles (argv: base run dir, e3b run
dir). ms/turn = share x the profiled step's pod CPU (cores) x 1000 / turns per second."""
import json, sys
from pathlib import Path
ROWS = [
    ("SigV4 signing (`signed_headers`)", ["_converse_wire.py:signed_headers"]),
    ("  of which botocore SigV4Auth / AWSRequest", ["botocore/auth.py", "botocore/awsrequest.py"]),
    ("  of which `_sigv4.sign_post`", ["noema/adapters/_sigv4.py"]),
    ("ConverseStream reader (`_ConverseRaw.next`)", ["converse.py:_ConverseRaw.next"]),
    ("  of which eventstream decoding (botocore / `_eventstream`)", ["botocore/eventstream.py", "noema/adapters/_eventstream.py"]),
    ("  of which `parse_event`", ["_converse_wire.py:parse_event"]),
    ("asyncpg release (`PoolConnectionHolder.release`)", ["asyncpg/pool.py:PoolConnectionHolder.release"]),
    ("  of which the reset query (`Connection.reset`)", ["asyncpg/connection.py:Connection.reset"]),
    ("decision member calls (`MemberCallRunner`)", ["MemberCallRunner.run"]),
    ("ingress (`IngressMiddleware.__call__`)", ["IngressMiddleware.__call__"]),
]
def load(run):
    stacks = []
    for f in sorted(Path(run).glob("profile-cpu-*.collapsed")):
        for line in f.read_text().splitlines():
            s, _, n = line.rpartition(" "); stacks.append((s, int(n)))
    step = json.loads((Path(run) / "results.json").read_text())["steps"][0]
    return stacks, step["resources"]["api_cpu_cores"] * 1000 / step["turns_per_s"]
(bs, bms), (es, ems) = load(sys.argv[1]), load(sys.argv[2])
bt, et = sum(n for _, n in bs), sum(n for _, n in es)
print(f"| Path (inclusive) | base share | base ms/turn | e3b share | e3b ms/turn |\n|---|---|---|---|---|")
for label, pats in ROWS:
    b = sum(n for s, n in bs if any(p in s for p in pats)); e = sum(n for s, n in es if any(p in s for p in pats))
    print(f"| {label} | {100*b/bt:.2f}% | {bms*b/bt:.2f} | {100*e/et:.2f}% | {ems*e/et:.2f} |")
print(f"| total (profiled step) | 100% ({bt:,} samples) | {bms:.1f} | 100% ({et:,} samples) | {ems:.1f} |")
