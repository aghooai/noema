"""`python -m noema.identity.cli`: the noema-admin operator CLI (IMPL-044)."""

from __future__ import annotations

import sys

from noema.identity.cli.noema_admin import main

if __name__ == "__main__":
    sys.exit(main())
