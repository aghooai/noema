#!/bin/bash
# snap.sh <name> : /tmp/noema-e3cpu/<name> = wave-e2 (git archive) + this role's changed files (FILES.txt), run.py patched
set -euo pipefail
NAME=$1; D=/tmp/noema-e3cpu/$NAME
rm -rf "$D"; mkdir -p "$D"
cd /codes/aghooai/noema
git archive wave-e2 | tar -x -C "$D"
while read -r f; do [ -n "$f" ] && mkdir -p "$D/$(dirname "$f")" && cp "$f" "$D/$f"; done < /tmp/noema-e3cpu/FILES.txt
cp /tmp/noema-e3cpu/FILES.txt "$D/SNAPSHOT.txt"
/codes/aghooai/noema/.venv/bin/python /tmp/noema-e3cpu/bin/patch_run.py "$D" >/dev/null
echo "$D: wave-e2 + $(grep -c . /tmp/noema-e3cpu/FILES.txt) files"
