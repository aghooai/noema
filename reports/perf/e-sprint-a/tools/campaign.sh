#!/bin/bash
# ABBA campaign: base a1 a2 a3 a3 a2 a1 base, 50 and 100 streams, 120 s windows
set -u
i=0
for v in base a1 a2 a3 a3 a2 a1 base; do
  i=$((i+1)); name="c$i-$v"
  /tmp/perfa/tools/run.sh /tmp/perfa/$v $name --plan 50:120,100:120 --results /tmp/perfa/results/$name.json > /tmp/perfa/runs/$name.out 2>&1
done
