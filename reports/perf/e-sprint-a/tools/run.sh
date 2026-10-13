#!/bin/bash
# usage: run.sh <snapshot dir> <run name> [run.py args...]
set -eu
snap=$1; name=$2; shift 2
cd "$snap"
for i in $(seq 1 120); do
  la=$(cut -d' ' -f1 /proc/loadavg)
  busy=$(pgrep -f 'tests\.load\.(serve|driver)' | wc -l)
  if awk "BEGIN{exit !($la < 8.0)}"; then break; fi
  sleep 10
done
echo "loadavg at start: $la"
others=$(pgrep -af 'tests\.load\.(serve|driver|mockserve)' | grep -v "$snap" | grep -v perfa || true)
if [ -n "$others" ]; then echo "OTHER LOAD RUN ACTIVE:"; echo "$others"; fi
PYTHONPATH="$snap/src:$snap" exec /codes/aghooai/noema/.venv/bin/python /tmp/perfa/tools/runwrap.py \
  --state ~/.noema/stacks/noema-perf-a.json --out /tmp/perfa/runs/$name --api-port 29790 --mock-port 29781 --api-cpus 14-15 --mock-cpus 8-9,32-33,10-11,34-35 --driver-cpus 12-13,36-37 --stack-cpus 4-7 "$@"
