"""Print the dev-proxy settings for a bootstrapped stack (tools/bootstrap_dev.py state file, mode 0600).

  python web/e2e/stack_env.py <state.json> url                     -> the first API endpoint (NOEMA_UI_API_URL)
  python web/e2e/stack_env.py <state.json> mock                    -> the mockbedrock endpoint (NOEMA_UI_E2E_MOCK_URL)
  python web/e2e/stack_env.py <state.json> keys [tenant] [project] -> alias=key,... (NOEMA_UI_API_KEYS); the aliases
                                                                      are the principal names (developer, tenant_admin,
                                                                      reviewer, auditor, ...); default t_acme / main
The keys go to the proxy's environment only; nothing is written to disk.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path


def main(argv: list[str]) -> int:
    if len(argv) < 3 or argv[2] not in ("url", "mock", "keys"):
        sys.stderr.write(__doc__ or "")
        return 2
    state = json.loads(Path(argv[1]).expanduser().read_text())
    if argv[2] == "url":
        sys.stdout.write(str(state["endpoints"]["api"][0]))
        return 0
    if argv[2] == "mock":
        sys.stdout.write(str(state["endpoints"]["mockbedrock"]))
        return 0
    tenant = argv[3] if len(argv) > 3 else "t_acme"
    project = argv[4] if len(argv) > 4 else "main"
    principals = state["tenants"][tenant]["projects"][project]["principals"]
    sys.stdout.write(",".join(f"{name}={p['key']}" for name, p in sorted(principals.items()) if "key" in p))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
