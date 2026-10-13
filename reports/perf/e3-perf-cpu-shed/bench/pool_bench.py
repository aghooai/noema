"""One unit of work = acquire, BEGIN, set_config(app.tenant_id/project_id, true), one SELECT, COMMIT, release, on
a 4-connection pool: with asyncpg's reset on release (PoolSettings default) vs without (app_pool_settings, Wave
E3). 20 concurrent workers x 500 units, best of 5; client CPU (process_time) and wall per unit; the reset queries
counted by a query logger. argv[1] = superuser DSN of the perf stack's Postgres."""
import asyncio, json, sys, time
from noema.core.secret import SecretStr
from noema.db.engine import Engine, PoolSettings
from noema.db.uow import PgDatabase

DSN = sys.argv[1]
async def run(reset: bool):
    engine = await Engine.create(PoolSettings(dsn=SecretStr(DSN), expected_role=None, max_size=4, reset_on_release=reset))
    db = PgDatabase(engine, lambda tx: tx)
    async def worker(n):
        for _ in range(n):
            async with db.tenant_uow("t_bench", project_id="prj_a") as tx:
                await tx.conn.fetchval("SELECT 1")
    await asyncio.gather(*(worker(25) for _ in range(20)))  # warm
    best = None
    for _ in range(5):
        c0, w0 = time.process_time(), time.perf_counter()
        await asyncio.gather(*(worker(25) for _ in range(20)))
        c, w = (time.process_time() - c0) / 500 * 1e6, (time.perf_counter() - w0) / 500 * 1e6
        best = (c, w) if best is None or c < best[0] else best
    await engine.close()
    return {"client_cpu_us_per_uow": round(best[0], 1), "wall_us_per_uow_4conns": round(best[1], 1)}
async def main():
    out = {}
    for reset in (True, False) * 4:
        out.setdefault("reset" if reset else "no_reset", []).append(await run(reset))
    print(json.dumps({k: min(v, key=lambda r: r["client_cpu_us_per_uow"]) | {"runs": [r["client_cpu_us_per_uow"] for r in v]} for k, v in out.items()}))
asyncio.run(main())
