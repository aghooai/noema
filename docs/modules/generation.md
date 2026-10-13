# noema.generation and noema.streaming (owner: generation, Wave C1; adapted in C2a)

Specs: spec-02 §4 (stream events), §7 (`TurnEventLog`, `SseWriter`), §10 (`GenerationEngine`, `ProfileRun`);
spec-04 §3, §4, §4.1 (T1-T19), §6, §10, §11; spec-11 §6 (attempt watchdog); IMPL-011, -012, -015, -016, -019, -020,
-021, -034, -084. Both packages import only `core`, `config`, `telemetry` (and `redis` for the mirror); they never
import each other or any other domain package.

## Public surface

### `noema.generation`
- `StreamingGenerationEngine(*, clock, ids, adapters, broker, health, retry_budget, estimator, cost, telemetry,
  rng=None)` implements `GenerationEngine`. `run(ctx, spec, prompt, *, hold, speculative, auto_commit="spec_agrees",
  failure_budget)` is an async context manager yielding a `StreamingProfileRun`.
  - `__aenter__`: `broker.get(ctx.tenant.tenant_id)` once (fail fast, IMPL-076) -> raises
    `TenantAccessMisconfigured`; a `best_of_n`/`fuse` profile raises `NoemaError(feature_not_available)` (Phase 2+,
    unreachable through P1-G1 and ingress); an unknown profile id raises the snapshot's `NoemaError(not_found)`.
  - `__aexit__` (shielded): attempts still live are stuck (the turn left without awaiting `result()`): they end as
    `cancelled(deadline)` (committed: `finish_reason cancelled`) with estimated usage, no frame, and count in
    `aghoo_stuck_attempts_total` (spec-11 §6). Exceptions of the body pass through unwrapped.
  - `rng`: the throttling jitter source (pass `random.Random(seed)` in tests; default `random.Random()`).
- `StreamingProfileRun` implements `ProfileRun`:
  - `events()`: one unbounded single-consumer stream of the openapi payloads `generation.started / committed /
    delta / cancelled / completed` and `error` (T12/T16/T17), ending after the run's last terminal event. Call it
    once (the orchestrator appends the events as given, inserting `route.confirmed` before `generation.committed`).
  - `first_text()`, `commit(reason)`, `cancel(reason)`, `result()` exactly as spec-02 §10; `commit`/`cancel` are
    idempotent no-ops after the run ended; `result()` lists the records in start order. Each `AttemptRecord`
    carries `first_text_at` (C2b): that attempt's own first text on the wall clock of `started_at`
    (`started_at + (first text - start)` in monotonic time), None without text; the orchestrator's `holdback_ms`.
  - `upstream_started` (`ProfileRun`, C2a; sync bool): True once any attempt of the run received `MessageStart`,
    reasoning, text or a stop; usage and error events do not count. The orchestrator's
    `decision.completed.arrived` rule (spec-04 §3): `before_generation` while False, else `during_holdback`.
  - `commit(reason)` with an attempt in B commits the one with the earliest text (race ties: start order); with
    none in B it arms the run, so the next first text commits with `reason` (T7). This includes
    `commit("holdback_deadline")` when `first_text()` came from an attempt that failed since: the fallback commits
    at its own first text with `holdback_deadline` (`holdback_ms` 0). Confirmed by orchestrator ruling R2
    (IMPL-012 amendment, spec-04 §3, spec-02 §10): the TTFT bound already fired, so the turn does not wait for S1.
  - `cancel("spec_changed")` after a commit is a no-op (T9 covers P/S/B; the losers already ended lost_race), so a
    later `cancel("client_abort")` or `cancel("deadline")` still ends the committed attempt (T18/T19).
  - extra (not in the Protocol): `attempts` (read-only inspection).
- `noema.generation.fsm`: `St`, `In`, `TRANSITIONS`, `step(state, input)`, `IllegalTransition` (the spec-04 §4.1
  attempt table as data; terminal states have no outgoing transition).
- `noema.generation.policy` (pure): `effective_effort`, `attempt_max_tokens`, `walk_error`, `estimate_usage`,
  `throttle_backoff_s`, `usage_wire`.

### `noema.streaming`
- `InProcessTurnEventLog(clock, *, mirror=None, orphan_after_s=3600)` implements `TurnEventLog`. `append` is sync
  and never awaits Redis: it assigns the id, serializes once (`SseFrame.wire`), wakes subscribers and queues the frame
  for the mirror. `subscribe` = backlog after `after`, then live, ends after `turn.completed` (or when the log is
  dropped). `seal(turn_id, ttl_s)` keeps the log `ttl_s` and queues the mirror `EXPIRE ttl_s`. `idle_seconds` =
  None while a local subscriber or a remote reader (`turn_attach` `a:` heartbeat) is attached, else the seconds
  since the last delivery anywhere. Extras: `sweep()` and `run()` (background: the mirror writer + a 30 s sweep of
  expired sealed logs and orphaned unsealed ones). At `seal` the log's frames are packed (`_SealedFrames`: the wires
  in one bytes blob plus offsets, a tuple of types): one GC-tracked object per sealed turn instead of one `SseFrame`
  per event (C2b perf smoke: the resume window kept 58% of the api's tracked heap as frames, and every full GC
  scanned them). Reads of a sealed log rebuild equal `SseFrame`s; a subscriber reading across the seal keeps the
  list it holds.
- `RedisEventMirror(redis, keys, clock, *, maxlen=20000, orphan_ttl_s=3600, block_ms=5000, beat_s=5.0,
  max_pending=100000, retry_s=1.0, follow_idle_s=660.0, batch_s=0.005)`: the best-effort Redis Streams copy
  (`RedisKeys.turn_events`, entry id `<frame id>-0`, fields `t`, `p`; `XADD MAXLEN ~ 20000`; `EXPIRE orphan_ttl_s`
  once, with a running turn's first batch, `ttl_s` at seal).
  `run()` writes the queue `batch_s` after each wake, so the frames of every running turn share one write (C2b perf
  smoke: one pipeline per frame, with a per-batch EXPIRE, was ~18% of the api's CPU at 50 streams; remote readers see
  frames up to 5 ms later). A write is one `EVALSHA` of `WRITE_LUA` (E sprint B; redis-py packing and parsing one
  XADD and its reply per frame was still ~8% of the api's CPU): it deletes the broken turns' keys, runs every turn's
  XADDs (same ids, fields, MAXLEN) and its EXPIRE, and returns only the turns whose writes were rejected; a batch is
  split into calls of at most `SCRIPT_FRAMES` (1,024) frames, whole turns each; `flush()`; `drain()` (graceful shutdown); `follow(turn_id, after=)` (the
  remote reader behind `attach_remote`: `XREAD BLOCK`, stops at a gap, at turn.completed, or when no entry arrived
  for `follow_idle_s` = the longest turn (Aghoo-Deadline-Ms max 600 s) + 60 s, so a follower of a pod that died
  mid-turn ends instead of waiting for the 3600 s orphan TTL). A turn whose write failed, or that appends while
  the queue holds `max_pending` ops, is marked broken in O(1): its key is deleted by the next batch, which also
  drops its queued ops, and nothing more is written, so other pods see "no mirror" (None) rather than a stream
  with gaps. A broken turn is forgotten once its seal has passed (no leak).
- `BackpressureSseWriter(clock)` implements `SseWriter` (256 events / 1 MiB queue; coalescing of consecutive
  `generation.delta` frames of one attempt at the high-water mark; `client_too_slow` after the queue stayed full,
  or one `send()` stayed blocked, for `too_slow_s` (a connected client that stops reading rarely fills the
  coalesced queue), then the id-less error frame with a 1 s budget; `: ping` `ping_s` after the last write; `send()`
  raising -> `disconnected`). It closes the frame source (`aclose`) on every exit. Diagnostics: `merged_frames`,
  `pings`, `too_slow`.
- Wake-ups on the per-frame path (E sprint B): the run's `EventQueue` consumer, every `_TurnLog` subscriber and both
  sides of a writer's queue wait on one `asyncio.Event` each (cleared before each wait), not on a new `anyio.Event`
  per wait (anyio's construction goes through sniffio's backend lookup on every frame). Every Noema process and test
  runs on asyncio; anyio cancel scopes cancel the waiting task as before.
- `noema.streaming.frames`: `encode_frame`, `frame_from_payload`, `merge_deltas`, `too_slow_frame`, `PING`. A frame
  keeps a right-sized `payload` sliced from its `wire` (an `orjson.dumps` result is backed by ~4 KiB): about
  0.4 KiB retained per delta frame instead of 4.6 KiB. Since C2a `noema.api.sse_frame.encode_frame` does the same;
  the two encoders give equal `SseFrame`s (and `too_slow_frame()` equals `encode_unnumbered` of the same event),
  pinned by `tests/unit/streaming/test_frames.py` (streaming cannot import `noema.api`, spec-01 §2 layers).

## Wiring in composition.py (C2)

```python
from noema.generation import StreamingGenerationEngine
from noema.streaming import BackpressureSseWriter, InProcessTurnEventLog, RedisEventMirror

mirror = RedisEventMirror(redis, keys, clock)                     # the process's bytes client, RedisKeys(ns)
events = InProcessTurnEventLog(clock, mirror=mirror)              # Services.events
sse = BackpressureSseWriter(clock)                                # Services.sse
generation = StreamingGenerationEngine(                           # Services.generation
    clock=clock, ids=ids, adapters=adapters, broker=broker, health=health, retry_budget=retry_budget,
    estimator=estimator, cost=cost, telemetry=telemetry)
# start(services, "api"):
services.runtime.spawn("streaming.event-log", events.run)         # mirror writer + local sweep (api only)
services.runtime.on_drain("streaming.mirror", lambda grace_s: mirror.drain())   # flush the last frames
```

All constructor defaults are the production values (the keyword arguments exist for tests).
`events.run()` must run in every process that appends (api); without it frames stay queued (bounded: at
`max_pending` the affected turns are marked broken, the local log is unaffected).

## Behavior summary (what the orchestrator can rely on)
- One attempt = one `Adapter.stream` call; `ModelRequest(call_kind="generation", retry="default", snapshot=ctx.snapshot,
  effort=<effective>, max_tokens=min(cap, (spec.max ?? member.max ?? cap ?? 4096) + reasoning_reserve))`; timeouts
  `ctx.snapshot.route_timeouts(member alias, effective effort)` with `deadline=ctx.deadline`. The connect/DNS/TLS
  retry (one, jittered, same attempt) is the adapter's (spec-04 §6 step 1).
- One `PromptBuilder.build(alias)` per attempt (media renegotiated per model); its degradations plus an effort
  `param_unsupported` one (`from: effort:<asked>`, `to: effort:<used|default>`) go on `generation.started` with
  `attempt_id` and `model` set. One `Reservation.consume("generation", alias)` per attempt.
- T2 skips (no attempt, no event, no budget): breaker open (`HealthRegistry.admit`), model not in
  `tenants.<t>.allowed_models`, `estimate(prompt) + max_tokens > context_window`.
- Walk: next member (fallback), then `routing.default_profile`'s first member (`route_id` = that profile), guarded
  by `AttemptBudget` (checked before, taken at start), `RetryBudget.try_spend`, not tenant_access; race members never
  spend budget; a throttle with a next member falls back at once, else one same-member retry after `retry_after_s`
  or `uniform(0, 0.5 s · 2^k)` (k = failure-driven attempts of this run) if the deadline leaves the route's
  first-answer timeout. That retry's guards run before the backoff (an `AttemptBudget` unit left, the deadline
  check, `RetryBudget.try_spend`, spent there and not again at the start), so a refused retry ends the walk at once
  instead of after `retry_after_s`. Exhausted:
  `error{all_routes_unavailable | internal_error | tenant_access_misconfigured}` ("No route could answer."); no
  attempt startable at all: `all_routes_unavailable`.
- After commit: `timeout:idle` -> `error{upstream_stalled}`, other failures -> by kind ("The answer stream failed.");
  an error after the attempt's `Stop` completes the answer instead. ctx.deadline (own watcher, adapter
  `timeout:deadline`, or `cancel("deadline")`) ends every live attempt by T19 without a frame and stops the walk;
  the orchestrator emits `error{deadline_exceeded}`.
- An adapter event that reaches an attempt already made terminal by another task (it lost the race, a cancel, or
  ctx.deadline fired at the same instant) is dropped: anyio does not deliver a cancel to a task whose wake-up is
  already pending, and the real adapter yields buffered events without suspending. The committed-delta hot path
  pays nothing for this check.
- Usage per attempt: the provider's `UsageEvent`, else IMPL-020 (`tokens_estimated`): input = the prompt estimate;
  output = ceil(text chars / 4) + ceil(reasoning chars / 4) + `platform.cost.cancel_overrun_tokens` when the stream
  was opened and cut before its `Stop` (pre-byte failures and completed streams get no overrun). Cost =
  `CostMeter.price(alias, tenant.home_region, usage, snapshot=ctx.snapshot)` (the turn's pinned catalog, IMPL-019),
  always `estimated` when the usage is; a meter raising `NoemaError` for the alias -> `Cost(0, estimated=True)`.
  The engine records no usage lines (the orchestrator does, from `result()`).

## Config keys read
`generation_profiles.<id>` (mode, members), `models.<alias>` (id, controls.effort, max_output_tokens_cap,
reasoning_reserve_tokens, context_window), `routing.default_profile`, `tenants.<t>.allowed_models`,
`platform.cost.cancel_overrun_tokens`, `ROUTE_TIMEOUTS_V1` via `route_timeouts`. Streaming reads no config: the
orchestrator passes `platform.events.resume_window_s` to `seal`.

## Telemetry
- Span `generation.attempt` per attempt (in the attempt's task, until it is terminal): `gen_ai.provider.name`,
  `gen_ai.operation.name`, `gen_ai.request.model` (Bedrock id), `gen_ai.conversation.id` (session), `aghoo.tenant`,
  `aghoo.profile` (route id), `aghoo.attempt.spec_signature`, `aghoo.conditioning.answer_mode`,
  `aghoo.degradation.reason`; at the end `aghoo.attempt.outcome`, `gen_ai.response.finish_reasons`,
  `gen_ai.usage.*`, `gen_ai.response.time_to_first_chunk`, `error.type` (the attempt's error code).
- Metrics: `aghoo_first_answer_seconds{profile, model, strategy}` (attempt start -> first text; strategy `gate` for
  auto_commit="gate", else `holdback`), `aghoo_wasted_output_tokens_total{profile, reason}` (non-committed attempts; the only emitter, Wave E),
  `aghoo_degradations_total{reason, model}` (the degradations on `generation.started`), `aghoo_stuck_attempts_total`.
- Breaker outcomes (`HealthRegistry.record` per attempt): ok (the provider answered; text-less stops included),
  throttled, timeout (`timeout:*`), error; tenant-access and invalid-request failures and attempts cancelled before
  any upstream event are not recorded. The TTFT sample (for the shared breaker's p95-TTFT rule) is left out (None)
  when the attempt's prompt estimate exceeds `TTFT_SIGNAL_MAX_INPUT_TOKENS` = 8,192 or it reasoned before its first
  text: prefill and effort are latency the tenant shapes (Wave E).
- Loggers `noema.generation`, `noema.streaming` (ids and codes only, never content).

## Tests (test ids)
- T-FSM-1: `tests/unit/generation/test_fsm_table.py` (every attempt row as data, terminal states, nothing else) and
  `tests/unit/generation/test_attempt_fsm.py` (the real engine through T1-T19 on scripted adapters).
- Golden G01-G12 (+ b variants), generation part: `tests/unit/generation/test_golden_generation.py` (the exact
  generation events and `db.attempts` rows of all 17 fixtures from the real engine, contract-valid payloads).
- P-SSE-1, generation part: `tests/unit/generation/test_generation_properties.py` (hypothesis, 400 runs, all times
  on a 50 ms grid so attempts, commit, cancel and deadline collide; every record's terminal frame and reason is
  checked exactly, none only for T19 and the committed attempt's error event).
- Same-instant interleavings and cancel order: `tests/unit/generation/test_same_instant.py` (race first texts at
  the same instant with hold=False, commit at a loser's text, deadline at a committed delta and at the first held
  text, cancel at a delta, spec_changed after commit then client_abort).
- Walk, requests, usage, telemetry, lifecycle: `test_walk.py`, `test_attempt_requests.py`, `test_policy.py`,
  `test_usage_and_telemetry.py` (incl. pricing with the pinned snapshot and home region), `test_engine_lifecycle.py`.
- `upstream_started`: `test_attempt_fsm.py` (T3 MessageStart/reasoning; usage and errors do not count, a text-less
  stop does) and the conformance case `test_upstream_started_flips_at_the_first_upstream_event`.
- Streaming: `tests/unit/streaming/` (frames incl. the retained-size bound, event log, writer backpressure incl. a
  blocked send, the mirror write queue on a recording write script: lazy breaking at `max_pending`, no `_broken`
  leak, a batch split into whole turns per call, a failed later call breaking only the turns it did not write)
  and
  `tests/integration/streaming/test_redis_mirror.py` (Valkey: ids, TTLs, live remote follow byte-identical (G12b),
  watchdog heartbeat (polled, no fixed sleep), gap, rejected write, a rejected write breaking only its turn in a
  shared script call, Redis down, the follower of a dead writer).
- Conformance registrations: `generation_engine/streaming` (`tests/unit/generation/conformance_impls.py`),
  `event_log/inprocess`, `sse_writer/backpressure` (`tests/unit/streaming/conformance_impls.py`),
  `event_log/redis-mirror` (`tests/integration/streaming/conformance_impls.py`; its `other_pod` awaits the first
  pod's `mirror.drain()` before `attach_remote`, see the file docstring).
- Commands: `make test MOD=generation`, `make test MOD=streaming`, `make test-conformance`.

## Known limits
- Golden `wasted_output_tokens` (G02, G06) come from the fake's chars/4 estimate and are no longer exact fields
  (request 2, C2a); the real engine gives G02 = 72 and G06 = 64.
- Stream timeouts are enforced by the adapter (spec-02 §4.1); the engine selects them per attempt and enforces only
  ctx.deadline itself.
- The cross-pod mirror is best effort: a duplicate arriving before the original's first frames reach Redis gets
  `attach_remote` None (the api answers 500, retryable). A turn longer than 20,000 frames loses its oldest mirrored
  frames (MAXLEN); a remote reader then stops at the gap.
- `events()` is single-consumer.
- With the queue at `max_pending` (Redis hung or far behind), every turn that appends is marked broken until the
  writer takes its next batch (breaking is O(1); nothing is rebuilt on the append path). The composition's Redis
  client has no socket timeout, so a hung Redis keeps the writer in one `execute()` and every new turn's mirror
  breaks; the local log is unaffected.
- A remote follower ends silently (no `turn.completed`) after `follow_idle_s` without entries, after a gap, or on a
  Redis error; the api then closes the stream.
- `client_too_slow` is detected on the writer's timer, which wakes at least every `ping_s`: exact when
  `ping_s <= too_slow_s` (the defaults 15 / 30), up to `ping_s - too_slow_s` late otherwise.
