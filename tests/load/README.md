# Load harness (spec-10 §5): driver, metrics, runner

Never part of `make check`. Measures one api process ("pod": uvicorn + uvloop + httptools, the image CMD) against
`tools/mockbedrock` with scripted, deterministic latencies, so `overhead = client-observed - mock-injected` is Noema.

| File | What |
|---|---|
| `driver.py` | anyio + httpx SSE client; one process runs N streams (50 per process); each stream loops turns on its own session (createSession; a new one every 10 turns) with every pre-commit key pinned (`sol-fast`, effort low, 600 tokens, `conditioning: off`), a `«nonce:<uuid>»` per turn (no L2 hit: the full router-default fan-out of 15 calls runs). One JSON line per turn (monotonic ns timestamps, server timings, committed reason). The first turn of each stream and every non-200 are validated with the `ContractValidator`, after the turn was timed. |
| `mockserve.py` | `MockBedrock` in-process (same server as the CLI) with an in-process record drain (no HTTP paging of `/_mock/requests` on the mock's loop during a run) and a lag sampler; the nonce is the LAST marker of the request's last message; `--reuse-port` lets several processes share one port. |
| `serve.py` | the api: the image CMD run programmatically, plus a lag sampler, a GC pause log, a GC heap census (SIGRTMIN), on-demand profilers (SIGUSR1/SIGUSR2: `cpu` = SIGPROF CPU-time stack sampler, `sample` = wall-clock thread sampler, `cprofile`), the createTurn tracer (`trace.py`) and `--dispatch-yields` (A/B of the decision dispatch yields). Runs on the host venv or, with `run.py --api-image`, inside the image (tests/load mounted read-only). |
| `lag.py` | the event-loop lag sampler (10 ms sleeps; uvloop timers have 1 ms granularity). |
| `stats.py` | joins turns with mock records by nonce (the earliest stream request by receive time); TTFT and e2e overhead with bootstrap 95% CIs (1,000 resamples), ingress/egress decomposition, the server's `decision_ms`/`decision_lead_ms`, spec-10 §5 validity (`validity.valid`: >= 99.9% of turns with `holdback_ms = 0` and `spec_agrees`, every driver and mock process with lag p99 <= 2 ms; plus the fan-out per turn), mock lateness, lag. |
| `run.py` | orchestration: Postgres and Valkey pinned (`docker update --cpuset-cpus`, 40-45), 4 mocks (2 Converse + 2 Mantle sharing the two ports, 32-39), the api (46-47; or `--api-image` = `docker run --cpuset-cpus` with the compose app settings), drivers (20-31) under `taskset`, a stepped plan `streams:hold_s,...`, 5 s resource samples (RSS, CPU, fds, threads, loadavg), `/metrics` per step, `results.json` with `provenance` (HEAD + a digest of the uncommitted code paths, active config version and digest, scenario digest, interpreter or image id, placement, load). |
| `stack.py` | `activate`: the load config (the bootstrap config with lifted tenant rate limits) on a dev stack. |
| `scenarios/overhead.yaml` | L1: decision members 50 ms; Sol first event 200 ms, first text 300 ms, 400 tokens at 60 tokens/s, stamped. |
| `scenarios/slow.yaml` | C3: L1 plus a heavy stream for turns tagged `«c3»` (2,700 chunks of 4,000 characters at 30 chunks/s, ~126 KB/s, 90 s). |
| `scenarios/decision.yaml` | L4: L1 with the decision members on the probe latency distributions (spec-10 §2.2) instead of 50 ms. |
| `slow.py` | C3 heavy-stream clients (`run.py --side slow:...` or `fast:...`): one long turn at a time on a fresh session, read at `--rate-bps` through a 16 KiB receive buffer (slow) or as fast as it arrives (fast); every complete frame contract-validated. |
| `sseprobe.py` | `serve.py --sse-probe-out` (`run.py --sse-probe`): the SSE writer's queue high-water mark, coalescing and the blocked-to-`client_too_slow` time per stream. |
| `trace.py` | createTurn wall-clock timelines inside the api (`serve --trace-out`): wrapped request-path functions plus Postgres/Redis round trips. |
| `test_harness.py` | self-tests of the pure parts (framing, stamps, nonce, join arithmetic, CIs, lag file, plan): `pytest tests/load/test_harness.py`. |
| `conftest.py`, `gatekit.py` | the stack gates (Wave E2, make/qa-gates.mk): an e2e compose stack of their own (`make qa-gates-up`), the gate config (lifted limits, the DeepSeek-only L5 profile), a raw-stream timed driver (`drive`: ramp-in, first-delta timing on the raw bytes, contract validation after the cell, driver loop lag), the mock ring reset and lateness (spec-10 §5 validity), the results file `NOEMA_QA_GATES_RESULTS`. Gate modules skip unless `NOEMA_QA_GATES=1`. |
| `test_holdback.py` | L2/L3 (`make qa-load-holdback`): client TTFT p95 <= max(T_d, TTFA) + 50 ms (agree) and <= T_d + TTFT(r*) + 100 ms (forced restart) per cell T_d {300, 900} x TTFA {300, 720, 2400}, n 200 at concurrency 50; Q8: `holdback_deadline` share and `served_spec_differs` rate. |
| `test_backup_tail.py` | L5 (`make qa-load-backup-tail`): DeepSeek-only `/v1/decisions` on the probe tail, p99 <= 1.5 s with backups, n >= 5,000. |
| `test_replay.py` | R4 (`make qa-replay`): a seeded replay set of multi-turn sessions (spec-restart share, waste share vs 06's P0 thresholds) and idempotent replay at load (no model call, no billing). |
| `test_hier_deadline.py` | Q9 (`make qa-hier-deadline`): the hierarchical `deadline_exceeded` share at `deadline_ms` 1,000 vs 3,000 on the probe latencies. |

## Running

```
# a stack of its own (base services only), seeded, with the load limits
V="NOEMA_PG_PORT=28732 NOEMA_MOTO_PORT=28750 NOEMA_VALKEY_PORT=28779"   # plus the other NOEMA_*_PORT of make/e2e.mk
env $V docker compose -p noema-perf -f deploy/compose/docker-compose.yml up -d --wait postgres valkey moto
UV_NO_SYNC=1 uv run --frozen --no-sync python -m tools.bootstrap_dev --compose-project noema-perf \
    --state ~/.noema/stacks/noema-perf.json
UV_NO_SYNC=1 uv run --frozen --no-sync python -m tests.load.stack activate --state ~/.noema/stacks/noema-perf.json

# a measurement (raw data outside the repo; the summary JSON may go to reports/perf/)
UV_NO_SYNC=1 uv run --frozen --no-sync python -m tests.load.run --state ~/.noema/stacks/noema-perf.json \
    --out /tmp/noema-perf/runs/<name> --plan 50:300,100:300,200:900 [--results reports/perf/<name>.json]
#   --profile cpu --profile-step 0   profile the api during step 0 (never a reported run)
#   --trace / --census               createTurn timelines / GC heap census (diagnosis)
#   --analyze-only                   re-analyze an existing --out directory
#   --api-image noema-api:<tag>      the api from the image (make image-api IMAGE_TAG=<tag>) instead of the venv
#   --dispatch-yields N              override DISPATCH_YIELDS in the api (A/B runs)
#   --api-procs N                    one pod of N api processes on --api-cpus sharing one listening socket (the
#                                    workers of `uvicorn --workers N`, WEB_CONCURRENCY=N; venv only): CPU, RSS and
#                                    fds are summed per pod, the api lag merges every process's samples
#   --api-env K=V                    extra environment of the api processes (repeatable)
#   --side flood:<tenant>:<n>:<step> n more driver streams of that tenant during one step (group "flood"; C5)
#   --side slow|fast:<n>:<step>[:bps] n heavy-stream clients during one step (tests/load/slow.py; C3)
#   --sse-probe                      the SSE writer probe in the api (C3)
#   --mock-quotas JSON               per-(AKID, model) Bedrock quotas in every mock process (C1)
# python -m tests.load.stack activate ... [--limit <t>:<key>=<int>] [--quota <t>:<alias>:<rpm>:<tpm>]   (C5, C1)
```

The drivers are closed-loop (spec-10 §5: N concurrent streams that loop turns): past the knee the reported latencies
are queueing at that concurrency (the throughput column is the capacity), not the response to an offered arrival
rate.

Record `/proc/loadavg` with every run (`samples.jsonl`, `resources.loadavg_1m` per step); the runs of
reports/perf/c2b-perf-smoke.md were started with the 1-minute loadavg below 8. A window is invalid (spec-10 §5) when
any driver or mock process has lag p99 > 2 ms or fewer than 99.9% of its turns have `holdback_ms = 0` with
`spec_agrees`: `results.json` `steps[].validity.valid` and `.checks`.
