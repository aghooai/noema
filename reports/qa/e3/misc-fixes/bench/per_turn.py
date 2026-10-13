"""R4 overhead sizing: Redis commands and Postgres units of work per turn on a running e2e stack, from the sampled
histogram counts (x 1/DATASTORE_SAMPLE_RATE) over all four api processes, net of an idle baseline of equal length."""
import asyncio, json, re, sys, time, uuid
import httpx

STATE = json.load(open(sys.argv[1]))
BASE = int(sys.argv[2])
N = int(sys.argv[3]) if len(sys.argv) > 3 else 400
KEY = STATE["tenants"]["t_acme"]["projects"]["main"]["principals"]["developer"]["key"]
API = [f"http://127.0.0.1:{BASE + 1}", f"http://127.0.0.1:{BASE + 2}"]
METRICS = [f"http://127.0.0.1:{BASE + p}/metrics" for p in (11, 12, 13, 14)]
PAT = re.compile(r'^(aghoo_(?:redis|db)_command_seconds)_count(\{[^}]*\})? ([0-9.e+]+)$', re.M)

async def scrape(c: httpx.AsyncClient) -> dict[str, float]:
    tot: dict[str, float] = {}
    for url in METRICS:
        text = (await c.get(url)).text
        for name, labels, v in PAT.findall(text):
            tot[name] = tot.get(name, 0.0) + float(v)
    return tot

async def turn(c: httpx.AsyncClient, i: int) -> int:
    body = {"input": [{"role": "user", "content": [{"type": "input_text", "text": f"What is the capital of France? ({uuid.uuid4().hex})"}]}], "stream": True}
    h = {"authorization": f"Bearer {KEY}", "aghoo-version": "2026-10-01", "content-type": "application/json"}
    async with c.stream("POST", API[i % 2] + "/v1/turns", content=json.dumps(body), headers=h, timeout=60) as r:
        async for _ in r.aiter_bytes():
            pass
        return r.status_code

async def main() -> None:
    async with httpx.AsyncClient() as c:
        await c.post(f"http://127.0.0.1:{BASE + 80}/_mock/scenario", content=open(sys.argv[4]).read(), headers={"content-type": "application/yaml"})
        a = await scrape(c); t0 = time.monotonic()
        sem = asyncio.Semaphore(4)
        async def one(i):
            async with sem:
                return await turn(c, i)
        codes = await asyncio.gather(*(one(i) for i in range(N)))
        dt = time.monotonic() - t0
        b = await scrape(c)
        await asyncio.sleep(dt)  # idle baseline of the same length
        d = await scrape(c)
    ok = sum(1 for x in codes if x == 200)
    print(f"turns {N} ok {ok} in {dt:.1f} s")
    for name in sorted(b):
        load = (b[name] - a.get(name, 0)) * 16
        idle = (d[name] - b[name]) * 16
        print(f"{name}: load {load:.0f} ops, idle {idle:.0f} ops -> {(load - idle) / max(1, ok):.1f} per turn")

asyncio.run(main())
