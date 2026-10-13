#!/bin/bash
# perf-cpu-shed (Wave E3) load runs: run.sh <snapshot dir> <run name> [run.py args...]
# Placement = the QA lead's E2 placement (api pod 46-47 = cores 22-23, siblings idle; stack 44-45; 8 mocks
# Converse 16-19 / Mantle 40-43; drivers 4-15,28-39), own stack noema-e3cpu-perf (ports 326xx).
# Serialized with the other perf agent: waits until no other tests.load.run is alive and the 1-minute loadavg < 4
# (after 40 min: < 8), re-checking after a random pause; records foreign load processes every 10 s during the run.
set -uo pipefail
SNAP=$1; NAME=$2; shift 2
OUT=/tmp/noema-e3cpu/runs/$NAME
limit=4
for i in $(seq 1 2400); do
  [ "$i" -gt 1200 ] && limit=8
  l=$(cut -d' ' -f1 /proc/loadavg)
  others=$(pgrep -f "^[^ ]*python[0-9.]* -m tests[.]load[.]run" | wc -l)
  if awk -v l="$l" -v m="$limit" 'BEGIN{exit !(l<m)}' && [ "$others" -eq 0 ]; then
    sleep 1
    others=$(pgrep -f "^[^ ]*python[0-9.]* -m tests[.]load[.]run" | wc -l)
    [ "$others" -eq 0 ] && break
  fi
  sleep 2
done
echo "start $(date -u +%FT%TZ) loadavg: $(cat /proc/loadavg)"
mkdir -p "$OUT"
( while sleep 10; do
    f=$(pgrep -af "^[^ ]*python[0-9.]* -m tests[.]load[.](run|driver|mockserve|serve)" | grep -v "/tmp/noema-e3cpu/runs/$NAME" | grep -v pgrep | wc -l)
    echo "$(date +%s) $(cut -d' ' -f1-3 /proc/loadavg) foreign=$f"
  done ) > "$OUT.monitor" 2>&1 &
MON=$!
cd "$SNAP"
PYTHONPATH="$SNAP/src:$SNAP" /codes/aghooai/noema/.venv/bin/python -m tests.load.run \
  --state ~/.noema/stacks/noema-e3cpu-perf.json --out "$OUT" --api-port 32690 --mock-port 32681 \
  --api-cpus 46-47 --api-procs 2 --stack-cpus 44-45 --mock-cpus 16,40,17,41,18,42,19,43 \
  --driver-cpus 4-15,28-39 "$@"
rc=$?
kill $MON 2>/dev/null
echo "end $(date -u +%FT%TZ) rc=$rc loadavg: $(cat /proc/loadavg)"
exit $rc
