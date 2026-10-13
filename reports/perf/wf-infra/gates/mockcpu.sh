#!/bin/bash
# every 5 s: the compose mock's CPU (cores, from /proc utime+stime of its python process) and the 1-min loadavg
name=$1; out=$2
pid=$(docker inspect -f '{{.State.Pid}}' "$name"); child=$(pgrep -P "$pid" | head -1)
tck=$(getconf CLK_TCK); prev=0; pt=$(date +%s.%N)
while sleep 5; do
  read -r u s < <(awk '{print $14, $15}' /proc/$child/stat 2>/dev/null) || break
  now=$(date +%s.%N); tot=$((u + s))
  [ "$prev" -gt 0 ] && echo "$(date +%T) cores=$(echo "($tot - $prev) / $tck / ($now - $pt)" | bc -l | cut -c1-5) aff=$(taskset -cp $child | awk '{print $NF}') load=$(cut -d' ' -f1 /proc/loadavg)"
  prev=$tot; pt=$now
done > "$out"
