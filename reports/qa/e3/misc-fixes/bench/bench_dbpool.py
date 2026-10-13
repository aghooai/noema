"""R4 overhead of `_DbPool.acquire` per unit of work: a no-op engine (the wrapper's own CPU), metrics off / on."""
import asyncio, statistics, time
from contextlib import asynccontextmanager
from noema import composition
from noema.core.clock import SystemClock
from noema.telemetry.setup import setup_telemetry
from noema.telemetry import OtelTelemetry

class NoopEngine:
    @asynccontextmanager
    async def acquire(self):
        yield object()

def pool(instruments):
    p = composition._DbPool(None, "ref", SystemClock(), instruments=instruments)  # type: ignore[arg-type]
    p._engine = NoopEngine()
    return p

async def run(p, n):
    c0 = time.process_time()
    for _ in range(n):
        async with p.acquire():
            pass
    return (time.process_time() - c0) / n * 1e6

async def main():
    tel = OtelTelemetry(setup_telemetry(service_name="bench", env="dev"), env="dev")
    off, on = pool(None), pool(tel.instruments)
    engine = NoopEngine()
    async def raw(n):
        c0 = time.process_time()
        for _ in range(n):
            async with engine.acquire():
                pass
        return (time.process_time() - c0) / n * 1e6
    res = {"engine only": [], "_DbPool no metrics": [], "_DbPool sampled 1/16": [], "_DbPool every unit": []}
    for _ in range(7):
        res["engine only"].append(await raw(50_000))
        res["_DbPool no metrics"].append(await run(off, 50_000))
        composition.DATASTORE_SAMPLE_RATE = 1 / 16
        res["_DbPool sampled 1/16"].append(await run(on, 50_000))
        composition.DATASTORE_SAMPLE_RATE = 1.0
        res["_DbPool every unit"].append(await run(on, 50_000))
        composition.DATASTORE_SAMPLE_RATE = 1 / 16
    for k, v in res.items():
        print(f"{k:22s} cpu {statistics.median(v):6.2f} us/unit (median of {len(v)})")
asyncio.run(main())
