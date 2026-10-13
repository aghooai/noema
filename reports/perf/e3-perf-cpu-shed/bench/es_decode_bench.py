"""Decode only: botocore EventStreamBuffer vs noema EventStreamDecoder over the same 104-message L1 body, one frame
per chunk; best of 7 x 300 bodies; pinned core. Needs the working tree on PYTHONPATH."""
import gc, json, time
from botocore.eventstream import EventStreamBuffer
from tools.mockbedrock.eventstream import event_frame
from noema.adapters._eventstream import EventStreamDecoder
frames = [event_frame("messageStart", {"role": "assistant"})]
frames += [event_frame("contentBlockDelta", {"contentBlockIndex": 0, "delta": {"text": "tok%d " % i}}) for i in range(100)]
frames += [event_frame("contentBlockStop", {"contentBlockIndex": 0}), event_frame("messageStop", {"stopReason": "end_turn"}),
           event_frame("metadata", {"usage": {"inputTokens": 31, "outputTokens": 100, "totalTokens": 131}, "metrics": {"latencyMs": 900}})]
def boto():
    b = EventStreamBuffer(); n = 0
    for f in frames:
        b.add_data(f)
        for m in b:
            m.headers; m.payload; n += 1
    return n
def lean():
    d = EventStreamDecoder(); n = 0
    for f in frames:
        d.add_data(f)
        while (m := d.next()) is not None:
            n += 1
    return n
assert boto() == lean() == 104
out = {}
for name, fn in (("botocore", boto), ("noema", lean)):
    best = []
    for _ in range(7):
        gc.collect(); gc.disable(); t = time.process_time_ns()
        for _ in range(300): fn()
        best.append((time.process_time_ns() - t) / 300 / 104 / 1000); gc.enable()
    out[name] = round(min(best), 2)
print(json.dumps({"us_per_message_best": out}))
