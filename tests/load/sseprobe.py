"""SSE writer probe for the C3 slow-client row (spec-10 §3 C3), installed in the api by `serve.py --sse-probe-out`:
wraps `noema.streaming.sse_writer` so that, per process, it records

- the queue high-water mark: the most frames and wire bytes any one stream's queue held after a push;
- the frames saved by coalescing (`_Stream.coalesce` return values) and how often it ran;
- every stream end by outcome, and for `client_too_slow` the seconds from the moment the stream first stayed blocked
  (the earlier of `full_since` and `send_since`, the timer's own reference) to the detection.

Harness instrumentation only (the writer's counters `merged_frames` / `too_slow` are per writer instance and the
high-water marks are not exported as metrics); `dump()` writes one JSON object.
"""

from __future__ import annotations

import json
import math
import time
from collections import Counter
from pathlib import Path
from typing import Any

from noema.streaming import sse_writer


class Probe:
    def __init__(self) -> None:
        self.max_events = 0
        self.max_bytes = 0
        self.max_frame_bytes = 0
        self.coalesce_runs = 0
        self.frames_saved = 0
        self.outcomes: Counter[str] = Counter()
        self.too_slow_after_s: list[float] = []

    def install(self) -> None:
        stream = sse_writer._Stream
        push, coalesce, finish = stream.push, stream.coalesce, sse_writer._finish
        probe = self

        def probed_push(st: Any, frame: Any) -> None:
            push(st, frame)
            probe.max_events = max(probe.max_events, len(st.queue))
            probe.max_bytes = max(probe.max_bytes, st.nbytes)
            probe.max_frame_bytes = max(probe.max_frame_bytes, len(frame.wire))

        def probed_coalesce(st: Any) -> int:
            saved = int(coalesce(st))
            probe.coalesce_runs += 1
            probe.frames_saved += saved
            return saved

        def probed_finish(st: Any, tg: Any, outcome: Any) -> None:
            if st.outcome is None:
                probe.outcomes[str(outcome)] += 1
                if outcome == "client_too_slow":
                    full = math.inf if st.full_since is None else st.full_since
                    blocked = math.inf if st.send_since is None else st.send_since
                    probe.too_slow_after_s.append(round(time.monotonic() - min(full, blocked), 4))
            finish(st, tg, outcome)

        stream.push = probed_push  # type: ignore[method-assign,assignment]
        stream.coalesce = probed_coalesce  # type: ignore[method-assign,assignment]
        sse_writer._finish = probed_finish

    def dump(self, path: Path) -> None:
        out = {
            "queue_high_water": {"events": self.max_events, "bytes": self.max_bytes},
            "max_frame_bytes": self.max_frame_bytes,
            "coalesce": {"runs": self.coalesce_runs, "frames_saved": self.frames_saved},
            "outcomes": dict(self.outcomes),
            "too_slow_after_s": self.too_slow_after_s,
        }
        path.write_text(json.dumps(out, indent=1))
