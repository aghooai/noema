#!/bin/sh
# SEC-SBX-01 (ADR-002 §2): run sandbox checks 1-10 inside nsjail jails. Run in the media-sandbox container, e.g.
#   docker compose -p noema-dev -f deploy/compose/docker-compose.yml run --rm media-sandbox /etc/noema-sandbox/selftest.sh
# Exit status: 0 all checks pass, 1 a check failed, 2 the jail could not start (privileges; see PRIVILEGES.md).
set -eu
here=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
exec /opt/noema-sandbox/venv/bin/python -I "$here/selftest.py" "$@"
