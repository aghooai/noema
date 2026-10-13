"""Run tests.load.run from a source snapshot (not a git checkout): code_version = a digest of the snapshot files."""
import hashlib
import sys

import tests.load.run as r


def code_version():
    h = hashlib.sha256()
    n = 0
    for d in ("src", "tests/load", "tools/mockbedrock"):
        for p in sorted((r.REPO / d).rglob("*")):
            if p.is_file() and "__pycache__" not in p.parts:
                h.update(str(p.relative_to(r.REPO)).encode() + b"\0" + hashlib.sha256(p.read_bytes()).digest())
                n += 1
    return {"git_head": "b85f318+snapshot", "snapshot": str(r.REPO), "files": n, "tree_digest": f"sha256:{h.hexdigest()}"}


r.code_version = code_version
sys.exit(r.main())
