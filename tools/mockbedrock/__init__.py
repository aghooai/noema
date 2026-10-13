"""The wire-level Bedrock mock (spec-10 §2): Converse JSON, ConverseStream over AWS eventstream, Mantle chat
completions (JSON and SSE), the probe quirks, scenario-driven answers, faults, barriers and a control API.

    async with MockBedrock(scenario=doc) as mock:          # in-process, 127.0.0.1 on an ephemeral port
        overrides = {"bedrock-runtime": mock.endpoint_url, "mantle": mock.mantle_url}
        ...
        mock.release("barrier"); mock.requests(); mock.unmatched()

`ThreadedMockBedrock` runs the same server on its own loop in a thread; `python -m tools.mockbedrock` is the CLI.
"""

from .app import MockBedrock, ScenarioSource
from .clock import MockClock, SystemClock, VirtualClock
from .state import Quota
from .threaded import ThreadedMockBedrock

__all__ = [
    "MockBedrock",
    "MockClock",
    "Quota",
    "ScenarioSource",
    "SystemClock",
    "ThreadedMockBedrock",
    "VirtualClock",
]
