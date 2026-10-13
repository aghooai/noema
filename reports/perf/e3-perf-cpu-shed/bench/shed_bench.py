"""CPU per refused createTurn: the edge shed (Wave E3) vs the route's shed after the full ingress (auth, body read,
gate, access log), on the unit-test app (fake authenticator: no Redis auth-failure check, so the route figure is a
lower bound of production's). Direct ASGI calls, best of 7 x 2,000 requests, one pinned core. Run from the tree."""
import asyncio, gc, json, logging, sys, time
import orjson
from tests.unit.api.test_edge_shed import TOKEN, _app
from tests.unit.turns.harness import USER_BODY
from noema.api.middleware.context import SERVICES_STATE_KEY

logging.basicConfig(stream=open("/dev/null", "w"), level=logging.INFO)  # handlers on, as in a process
BODY = orjson.dumps({**USER_BODY, "session_id": "ses_1", "stream": False})

async def bench(edge: bool) -> dict:
    h, app, authn, slots = _app({"t_acme": 4})
    app.edge_shed = edge
    await h.world.runtime.open()
    async with app.router.lifespan_context(app) as state:
        st = dict(state or {}) if state else {SERVICES_STATE_KEY: h.service.svc}
        headers = [(b"host", b"t"), (b"authorization", f"Bearer {TOKEN}".encode()), (b"content-type", b"application/json"),
                   (b"content-length", str(len(BODY)).encode())]
        sent = []
        async def receive():
            return {"type": "http.request", "body": BODY, "more_body": False}
        async def send(m):
            if m["type"] == "http.response.start": sent.append(m["status"])
        def scope():
            return {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": "POST", "scheme": "http",
                    "path": "/v1/turns", "raw_path": b"/v1/turns", "query_string": b"", "headers": headers,
                    "client": ("127.0.0.1", 5000), "server": ("t", 80), "state": dict(st), "app": app}
        for _ in range(200):
            await app(scope(), receive, send)
        assert set(sent) == {429}, set(sent)
        best = []
        for _ in range(7):
            gc.collect(); t = time.process_time()
            for _ in range(2000):
                await app(scope(), receive, send)
            best.append((time.process_time() - t) / 2000 * 1e6)
    return {"us_per_request_best": round(min(best), 1), "authenticated": authn.lookups}

async def main():
    print(json.dumps({"edge": await bench(True), "route": await bench(False)}))
asyncio.run(main())
