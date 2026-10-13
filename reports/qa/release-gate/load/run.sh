#!/bin/bash
# Release-gate load runs on the own stack noema-rg-perf (postgres 22032, moto 22050, valkey 22079; api 22090 + metrics
# 22091/22092, mocks 22081/22082), always the api pod from the image (--api-image noema-api:rgate, 2 processes).
# Placement = E3 QA lead / Wave F infra-perf: api pod CPUs 46-47, stack 44-45, 8 mock processes (Converse 16-19,
# Mantle 40-43), drivers 4-15,28-39. Waits until no other tests.load.run is alive and the 1-minute loadavg < 6
# (after 20 min: < 8); records loadavg and foreign load processes every 10 s in <run>.monitor.
set -uo pipefail
NAME=$1; shift
OUT=/tmp/noema-rg/runs/$NAME
limit=6
for i in $(seq 1 1800); do
  [ "$i" -gt 600 ] && limit=8
  l=$(cut -d' ' -f1 /proc/loadavg)
  others=$(pgrep -f "^[^ ]*python[0-9.]* -m tests[.]load[.]run" | wc -l)
  if awk -v l="$l" -v m="$limit" 'BEGIN{exit !(l<m)}' && [ "$others" -eq 0 ]; then break; fi
  sleep 2
done
echo "start $(date -u +%FT%TZ) loadavg: $(cat /proc/loadavg) digest $(/tmp/noema-rg/bin/digest.sh)"
mkdir -p "$OUT"
( while sleep 10; do
    f=$(pgrep -af "^[^ ]*python[0-9.]* -m tests[.]load[.](run|driver|mockserve|serve)" | grep -v "/tmp/noema-rg/runs/$NAME" | grep -v pgrep | wc -l)
    echo "$(date +%s) $(cut -d' ' -f1-3 /proc/loadavg) foreign=$f"
  done ) > "$OUT.monitor" 2>&1 &
MON=$!
cd /codes/aghooai/noema
UV_NO_SYNC=1 uv run --frozen --no-sync python -m tests.load.run --state ~/.noema/stacks/noema-rg-perf.json \
  --out "$OUT" --api-port 22090 --mock-port 22081 --api-cpus 46-47 --api-procs 2 --api-image noema-api:rgate \
  --stack-cpus 44-45 --mock-cpus 16,40,17,41,18,42,19,43 --driver-cpus 4-15,28-39 "$@"
rc=$?
kill $MON 2>/dev/null
echo "end $(date -u +%FT%TZ) rc=$rc loadavg: $(cat /proc/loadavg) digest $(/tmp/noema-rg/bin/digest.sh)"
exit $rc
