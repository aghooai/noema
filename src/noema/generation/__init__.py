"""noema.generation (spec-01 §1): the generation engine, profile runs (single, fallback, race), the attempt state
machine, the holdback buffer and cancellation estimates (spec-02 §10, spec-04 §3, §4.1, §6)."""

from .engine import StreamingGenerationEngine
from .fsm import TRANSITIONS, IllegalTransition, In, St, step
from .run import GenerationDeps
from .walk import StreamingProfileRun

__all__ = [
    "TRANSITIONS",
    "GenerationDeps",
    "IllegalTransition",
    "In",
    "St",
    "StreamingGenerationEngine",
    "StreamingProfileRun",
    "step",
]
