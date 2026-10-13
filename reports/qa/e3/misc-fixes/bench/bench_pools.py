import asyncio, statistics, time
import redis.asyncio
from redis.asyncio.connection import ConnectionPool, BlockingConnectionPool
from redis.exceptions import MaxConnectionsError
URL = "redis://127.0.0.1:26379/0"

class FastGet(BlockingConnectionPool):
    async def get_connection(self, *a, **kw):
        if self.can_get_connection():
            try:
                return await ConnectionPool.get_connection(self)
            except MaxConnectionsError:
                pass
        return await super().get_connection()

class FastBoth(FastGet):
    async def release(self, connection):
        if not self._condition._waiters:
            return await ConnectionPool.release(self, connection)
        return await super().release(connection)

async def run(client, n):
    await client.set("bench:r4", b"x")
    c0 = time.process_time()
    for _ in range(n):
        await client.get("bench:r4")
    return (time.process_time() - c0) / n * 1e6

async def main():
    clients = {
        "ConnectionPool (head)": redis.asyncio.Redis.from_url(URL),
        "Blocking": redis.asyncio.Redis.from_pool(BlockingConnectionPool.from_url(URL, max_connections=256, timeout=0.5)),
        "Blocking fast get": redis.asyncio.Redis.from_pool(FastGet.from_url(URL, max_connections=256, timeout=0.5)),
        "Blocking fast get+release": redis.asyncio.Redis.from_pool(FastBoth.from_url(URL, max_connections=256, timeout=0.5)),
    }
    res = {k: [] for k in clients}
    for _ in range(7):
        for k, c in clients.items():
            res[k].append(await run(c, 20000))
    for k, v in res.items():
        print(f"{k:28s} cpu {statistics.median(v):7.2f} us/op")
asyncio.run(main())
