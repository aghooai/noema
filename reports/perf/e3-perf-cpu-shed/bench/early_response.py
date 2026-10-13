"""Does an early 429 (sent before the request body is read, as the edge shed does) keep HTTP/1.1 keep-alive intact
under uvicorn + httptools? 3,000 POSTs with a 600-byte body over 4 pooled keep-alive connections; the body is
sent in a second write half the time (client side: h11 via httpx may coalesce; so also a raw-socket variant)."""
import asyncio, socket, sys, time
import httpx, uvicorn

async def app(scope, receive, send):
    if scope["type"] != "http":
        return
    await send({"type": "http.response.start", "status": 429, "headers": [(b"content-type", b"application/json"), (b"retry-after", b"1"), (b"content-length", b"2")]})
    await send({"type": "http.response.body", "body": b"{}", "more_body": False})

async def main():
    config = uvicorn.Config(app, host="127.0.0.1", port=0, http="httptools", loop="asyncio", log_level="warning")
    server = uvicorn.Server(config)
    task = asyncio.create_task(server.serve())
    while not server.started:
        await asyncio.sleep(0.01)
    port = server.servers[0].sockets[0].getsockname()[1]
    errors = 0; statuses = {}
    async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}", limits=httpx.Limits(max_connections=4)) as c:
        async def one(i):
            nonlocal errors
            try:
                r = await c.post("/v1/turns", content=b"x" * 600, headers={"content-type": "application/json"})
                statuses[r.status_code] = statuses.get(r.status_code, 0) + 1
            except httpx.TransportError:
                errors += 1
        for k in range(300):
            await asyncio.gather(*(one(i) for i in range(10)))
    # raw socket: headers first, the body in a later write after the response arrived; then a second request
    raw_ok = 0
    for _ in range(200):
        s = socket.create_connection(("127.0.0.1", port))
        s.sendall(b"POST /v1/turns HTTP/1.1\r\nHost: t\r\nContent-Type: application/json\r\nContent-Length: 600\r\n\r\n")
        await asyncio.sleep(0.002)
        s.sendall(b"y" * 600)
        s.sendall(b"POST /v1/turns HTTP/1.1\r\nHost: t\r\nContent-Length: 3\r\n\r\nabc")
        s.setblocking(False)
        data, t0 = b"", time.monotonic()
        while data.count(b"HTTP/1.1 429") < 2 and time.monotonic() - t0 < 1.0:
            try:
                data += s.recv(65536)
            except BlockingIOError:
                await asyncio.sleep(0.002)
        raw_ok += data.count(b"HTTP/1.1 429")
        s.close()
    server.should_exit = True
    await task
    print({"httpx_statuses": statuses, "httpx_transport_errors": errors, "raw_two_responses_per_conn": raw_ok, "of": 400})

asyncio.run(main())
