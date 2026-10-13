"""noema.adapters (spec-01 §1, spec-02 §4): the Bedrock model adapters (`bedrock-converse` over the lean unary
path, `bedrock-mantle-chat` over it and httpx streams), their capabilities and error classification, the
`AdapterRegistry`, and the resilience layer (circuit breakers, retry budget, stream timeouts). See
docs/modules/adapters.md."""

from noema.adapters._base import SecurityEventSink
from noema.adapters.capabilities import PROBED, capabilities_of, probe_discrepancies, probe_unverified
from noema.adapters.classify import classify
from noema.adapters.converse import ConverseAdapter
from noema.adapters.mantle import MantleAdapter
from noema.adapters.registry import AdapterRegistryImpl
from noema.adapters.resilience import (
    BreakerPolicy,
    BreakerRegistry,
    RetryBudgetPolicy,
    SlidingRetryBudget,
)

__all__ = [
    "PROBED",
    "AdapterRegistryImpl",
    "BreakerPolicy",
    "BreakerRegistry",
    "ConverseAdapter",
    "MantleAdapter",
    "RetryBudgetPolicy",
    "SecurityEventSink",
    "SlidingRetryBudget",
    "capabilities_of",
    "classify",
    "probe_discrepancies",
    "probe_unverified",
]
