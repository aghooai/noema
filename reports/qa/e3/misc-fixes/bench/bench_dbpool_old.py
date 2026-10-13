import asyncio, statistics, time
from contextlib import asynccontextmanager
from noema import composition
from noema.core.clock import SystemClock
print(composition.__file__)
class NoopEngine:
    @asynccontextmanager
    async def acquire(self):
        yield object()
async def main():
    p = composition._DbPool(None, "ref", SystemClock())
    p._engine = NoopEngine()
    res = []
    for _ in range(7):
        c0 = time.process_time()
        for _ in range(50_000):
            async with p.acquire():
                pass
        res.append((time.process_time() - c0) / 50_000 * 1e6)
    print(f"_DbPool (HEAD)         cpu {statistics.median(res):6.2f} us/unit (median of 7)")
asyncio.run(main())
