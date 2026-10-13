import time, orjson, sys
sys.path.insert(0, "/codes/aghooai/noema")
from tests.unit.adapters.emulator_wire import event_frame
from botocore.eventstream import EventStreamBuffer, EventStream
from botocore.parsers import EventStreamJSONParser
from botocore.model import ServiceModel
from noema.adapters.converse import _loaded_data_loader
loader = _loaded_data_loader()
sm = ServiceModel(loader.load_service_model("bedrock-runtime", "service-2"), service_name="bedrock-runtime")
out = sm.operation_model("ConverseStream").output_shape
stream_shape = out.members["stream"]
frames = [event_frame("messageStart", {"role": "assistant"})] + [event_frame("contentBlockDelta", {"contentBlockIndex": 0, "delta": {"text": "«ts:123456789» tok"}}) for _ in range(100)] + [event_frame("contentBlockStop", {"contentBlockIndex": 0}), event_frame("messageStop", {"stopReason": "end_turn"}), event_frame("metadata", {"usage": {"inputTokens": 5, "outputTokens": 400, "totalTokens": 405}, "metrics": {"latencyMs": 7}})]
data = b"".join(frames)
parser = EventStreamJSONParser()
N = 200
t = time.process_time()
for _ in range(N):
    class Raw:
        def stream(self): return iter([data])
    es = EventStream(Raw(), stream_shape, parser, "ConverseStream")
    events = list(es)
bot = (time.process_time() - t) / N
print("botocore EventStream (sync) per turn of 104 events:", round(bot * 1e3, 3), "ms", events[1])
t = time.process_time()
for _ in range(N):
    buf = EventStreamBuffer(); buf.add_data(data)
    ev = []
    for msg in buf:
        h = msg.headers
        ev.append({h[":event-type"]: orjson.loads(msg.payload)})
lean = (time.process_time() - t) / N
print("EventStreamBuffer + orjson per turn:", round(lean * 1e3, 3), "ms", ev[1])
