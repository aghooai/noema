"""R4 overhead: CPU per Redis command, plain redis.asyncio client vs the metered composition client (same pool kind)."""
import asyncio, os, statistics, sys, time
import redis.asyncio
from noema import composition
from noema.core.clock import SystemClock
from noema.telemetry.setup import setup_telemetry
from noema.telemetry import OtelTelemetry

URL = os.environ.get("URL", "redis://127.0.0.1:26379/0")
N = int(os.environ.get("N", "20000"))

async def run(client, n):
    await client.set("bench:r4", b"x")
    t0, c0 = time.perf_counter(), time.process_time()
    for _ in range(n):
        await client.get("bench:r4")
    return (time.perf_counter() - t0) / n * 1e6, (time.process_time() - c0) / n * 1e6

async def run_pipe(client, n):
    t0, c0 = time.perf_counter(), time.process_time()
    for _ in range(n):
        async with client.pipeline(transaction=False) as p:
            p.get("bench:r4"); p.get("bench:r4")
            await p.execute()
    return (time.perf_counter() - t0) / n * 1e6, (time.process_time() - c0) / n * 1e6

async def main():
    settings = composition.process_settings("dev", {"NOEMA_REDIS_URL": URL})
    tel = OtelTelemetry(setup_telemetry(service_name="bench", env="dev"), env="dev")
    plain_pool = redis.asyncio.BlockingConnectionPool.from_url(URL, max_connections=256, timeout=0.5)
    plain = redis.asyncio.Redis.from_pool(plain_pool)
    head = redis.asyncio.Redis.from_url(URL, decode_responses=False)  # wave-e2: unbounded ConnectionPool
    metered = composition._redis_client(settings, composition._RedisMetrics(tel.instruments, SystemClock()))
    unmetered = composition._redis_client(settings, None)
    res = {"head": [], "plain": [], "metered": [], "metered-off": [], "pipe-head": [], "pipe-plain": [], "pipe-metered": []}
    for _ in range(int(os.environ.get("ROUNDS", "7"))):
        res["head"].append(await run(head, N))
        res["plain"].append(await run(plain, N))
        res["metered"].append(await run(metered, N))
        res["metered-off"].append(await run(unmetered, N))
        res["pipe-head"].append(await run_pipe(head, N // 4))
        res["pipe-plain"].append(await run_pipe(plain, N // 4))
        res["pipe-metered"].append(await run_pipe(metered, N // 4))
    for k, v in res.items():
        wall = statistics.median(x[0] for x in v); cpu = statistics.median(x[1] for x in v)
        print(f"{k:13s} wall {wall:7.2f} us/op  cpu {cpu:7.2f} us/op (median of {len(v)})")
    for c in (head, plain, metered, unmetered):
        await c.aclose()

asyncio.run(main())
