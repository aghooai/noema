#!/bin/bash
# queue.sh <queue file>: runs each line "<name> <snapshot> <run.py args...>" through run.sh, one after the other;
# a line is consumed (moved to <file>.done) when its run ends, so lines can be appended while the queue runs.
Q=$1
while true; do
  line=$(grep -v '^\s*$' "$Q" | head -1)
  [ -z "$line" ] && break
  grep -vxF "$line" "$Q" > "$Q.tmp"; mv "$Q.tmp" "$Q"
  set -- $line; name=$1; snap=$2; shift 2
  /tmp/noema-e3cpu/bin/run3.sh "$snap" "$name" "$@" --results /tmp/noema-e3cpu/results/$name.json > /tmp/noema-e3cpu/logs/$name.log 2>&1
  echo "$(date -u +%FT%TZ) rc=$? $line" >> "$Q.done"
done
