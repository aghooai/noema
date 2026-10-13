"""CPU of the ConverseStream reader's decoding per event: `_ConverseRaw.next()` over a 100-delta mock body (the L1
stream: messageStart, 100 contentBlockDelta, contentBlockStop, messageStop, metadata), chunked one frame per chunk
(as the wire mock writes them) and in 4 KiB chunks. Best of 7 x 200 streams, one pinned core.
Run with PYTHONPATH=<tree>/src:<tree> from that tree."""
import asyncio, gc, json, time
from tests.fakes.clock import FakeClock
from tests.unit.adapters.lean_reference import converse_model
from tools.mockbedrock.eventstream import event_frame
from noema.adapters.converse import _ConverseRaw

frames = [event_frame("messageStart", {"role": "assistant"})]
frames += [event_frame("contentBlockDelta", {"contentBlockIndex": 0, "delta": {"text": "tok%d " % i}}) for i in range(100)]
frames += [event_frame("contentBlockStop", {"contentBlockIndex": 0}), event_frame("messageStop", {"stopReason": "end_turn"}),
           event_frame("metadata", {"usage": {"inputTokens": 31, "outputTokens": 100, "totalTokens": 131}, "metrics": {"latencyMs": 900}})]
whole = b"".join(frames)
CHUNKINGS = {"frame": frames, "4k": [whole[i:i + 4096] for i in range(0, len(whole), 4096)]}

class Body:
    def __init__(self, chunks): self.c = list(chunks); self.i = 0
    async def chunk(self):
        if self.i < len(self.c):
            self.i += 1; return self.c[self.i - 1]
        return b""
    def close(self): pass

async def one(model, chunks):
    raw = _ConverseRaw(Body(chunks), model, FakeClock(), lambda _: None)
    n = 0
    while (ev := await raw.next()) is not None:
        raw.feed(ev); n += 1
    return n

async def main():
    model = converse_model()
    out = {}
    for name, chunks in CHUNKINGS.items():
        assert await one(model, chunks) == 104
        best = []
        for _ in range(7):
            gc.collect(); gc.disable()
            t = time.process_time_ns()
            for _ in range(200):
                await one(model, chunks)
            best.append((time.process_time_ns() - t) / 200 / 104 / 1000)
            gc.enable()
        out[name] = {"us_per_event_best": round(min(best), 2), "runs": [round(b, 2) for b in best]}
    print(json.dumps(out))

asyncio.run(main())
