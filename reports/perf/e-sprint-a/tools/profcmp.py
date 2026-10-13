"""Inclusive CPU shares and ms per turn of selected frames: python profcmp.py base_dir base_json after_dir after_json."""
import json, sys
def load(path):
    stacks = []
    for line in open(path):
        stack, n = line.rsplit(" ", 1)
        stacks.append((stack.split(";"), int(n)))
    return sum(n for _, n in stacks), stacks
def share(stacks, needle):
    return sum(n for frames, n in stacks if any(needle in f for f in frames))
def cpu(path):
    s = json.load(open(path))["steps"][0]
    return s["resources"]["api_cpu_cores"] * 1000 / s["turns_per_s"]
FRAMES = [
    ("decision member calls", "MemberCallRunner._call"),
    ("Converse complete()", "ConverseAdapter.complete"),
    ("Mantle complete()", "MantleAdapter.complete"),
    ("aiobotocore _make_api_call", "AioBaseClient._make_api_call"),
    ("httpx send", "AsyncClient.send"),
    ("lean unary transport", "AiohttpUnary.post"),
    ("SigV4 signing (lean)", "signed_headers"),
    ("generation stream events", "StreamSession._events"),
    ("aiobotocore event stream", "AioEventStream.__anext__"),
    ("lean ConverseStream reader", "_ConverseRaw.next"),
    ("decision entry", "RunContext.__aenter__"),
    ("createTurn ingress (route)", "create_turn"),
    ("Postgres (asyncpg Connection)", "asyncpg/connection.py"),
    ("Redis event mirror", "RedisEventMirror.run"),
    ("SSE writer", "BackpressureSseWriter._writer"),
    ("OTel spans", "OtelTelemetry._span"),
    ("asyncio task bookkeeping (WeakSet._remove)", "WeakSet.__init__.<locals>._remove"),
]
(bd, bj, ad, aj) = sys.argv[1:5]
bt, bi = load(bd); at, ai = load(ad); bc, ac = cpu(bj), cpu(aj)
print(f"| Frame (inclusive) | base share | base ms/turn | final share | final ms/turn |")
print("|---|---|---|---|---|")
for label, needle in FRAMES:
    b = share(bi, needle)
    a = share(ai, needle)
    print(f"| {label} | {b/bt*100:.1f}% | {b/bt*bc:.1f} | {a/at*100:.1f}% | {a/at*ac:.1f} |")
print(f"| total (profiled run) | 100% | {bc:.1f} | 100% | {ac:.1f} |")
