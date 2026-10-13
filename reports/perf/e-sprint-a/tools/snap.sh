#!/bin/bash
# usage: snap.sh <name> <repo-relative files...>: a copy of the baseline snapshot plus these files from the live tree
set -eu
name=$1; shift
rm -rf /tmp/perfa/$name; cp -a /tmp/perfa/base /tmp/perfa/$name
for f in "$@"; do mkdir -p "/tmp/perfa/$name/$(dirname $f)"; cp "/codes/aghooai/noema/$f" "/tmp/perfa/$name/$f"; done
find /tmp/perfa/$name -name __pycache__ -prune -exec rm -rf {} +
echo "snapshot /tmp/perfa/$name: base + $*"
