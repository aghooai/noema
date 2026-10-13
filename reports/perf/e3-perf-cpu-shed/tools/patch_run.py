"""Patch a snapshot's tests/load/run.py: code_version() hashes the snapshot's files (no git in /tmp snapshots)."""
import sys
from pathlib import Path

path = Path(sys.argv[1]) / "tests" / "load" / "run.py"
text = path.read_text()
old = 'def code_version() -> dict[str, Any]:'
new = '''def code_version() -> dict[str, Any]:
    digest = hashlib.sha256()
    for top in CODE_PATHS:
        base = REPO / top
        files = [base] if base.is_file() else sorted(p for p in base.rglob("*") if p.is_file() and "__pycache__" not in p.parts)
        for p in files:
            digest.update(str(p.relative_to(REPO)).encode() + b"\\0" + hashlib.sha256(p.read_bytes()).digest())
    return {"snapshot": str(REPO), "base": "wave-e2 1a28933 (perf-cpu-shed snapshot)", "paths": list(CODE_PATHS), "tree_digest": f"sha256:{digest.hexdigest()}"}


def _git_code_version() -> dict[str, Any]:'''
assert old in text
path.write_text(text.replace(old, new, 1))
print("patched", path)
