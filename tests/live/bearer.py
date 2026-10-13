"""Early pytest plugin of the live smoke: `pytest -p tests.live.bearer` imports this module before the root conftest
scrubs every `AWS_*` variable (spec-10 §1.1), so the Mantle bearer token of the developer's shell
(`AWS_BEARER_TOKEN_BEDROCK`) survives for the one fixture that hands it to identity's `AmbientBroker` (the
`NOEMA_CREDENTIALS_BROKER=ambient` live turn). The value is held in memory only, never logged or written."""

from __future__ import annotations

import os
from typing import Final

from noema.core.secret import SecretStr

ENV: Final = "AWS_BEARER_TOKEN_BEDROCK"
_raw = os.environ.get(ENV)
TOKEN: Final[SecretStr | None] = SecretStr(_raw, register=True) if _raw else None
del _raw
