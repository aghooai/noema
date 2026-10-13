# Implementation decisions: decision engine (Wave A)

> **Superseded for implementation: the canonical file is [`../IMPL-DECISIONS.md`](../IMPL-DECISIONS.md)** (lead-architect consolidation, 2026-10-09). This file is kept as-is for traceability. Ids map as `IMPL-DEC-n → IMPL-NNN` (see the Traceability table there). Where this file and the canonical file differ, the canonical file wins. The material changes:
>
> | Here | Canonical | Change |
> |---|---|---|
> | IMPL-DEC-5 | IMPL-027 + **IMPL-036** | Hierarchical needed: native and turns → 422 **`feature_not_available`**; facade and Jev → 422 `unsupported_parameter` (critic 2/29). The doc-patch text changes accordingly |
> | IMPL-DEC-7 | IMPL-047 | `x_aghoo` keys = client-visible names (the given name, else `q{i}`); the stored result keeps the internal names (critic 10/30) |
> | IMPL-DEC-9 | IMPL-031 | No "unresolvable default" 400: the loader fills `lightweight-model-decide`. 403 for unknown or non-allowlisted on **both** surfaces; the facade contract description is patched (critic 4/5/35) |
> | IMPL-DEC-10 | IMPL-049 | Jev `score` and `confidence`, and facade `confidence`, computed over each format's own emitted map, n = K (critic 6/7) |
> | IMPL-DEC-11 | IMPL-050 | Patch `doc` (04:L101-102, L132); `ModelRequest.retry` (critic 14/47) |
> | IMPL-DEC-14 | IMPL-053 + IMPL-029a | The spec text is the canonical `@1`; 08 §3.2 doc-patched (critic 18) |
> | IMPL-DEC-16 | IMPL-020 | Cancelled calls: `cost_estimated` too; output = `max_tokens` (critic 24/53) |
> | IMPL-DEC-17 | IMPL-055 | `calibrated_distribution` whenever members are emitted (critic 9) |
> | IMPL-DEC-18 | IMPL-056 | Bullet 1 (remote URL → 400 `unsupported_parameter`) and the 03:L56 patch **withdrawn**: 400 `schema_violation` (critic 3/34) |
> | IMPL-DEC-19 | IMPL-022 + IMPL-057 | Insert failure → outbox, never fails the turn; the AccessDenied deviation flagged (critic 21/44) |
> | IMPL-DEC-20 | IMPL-004 | Extra put gates (budget-skipped escalation, deadline cap), 20 ms fail-open, `cache_savings` line (critic 17/44/61) |
> | IMPL-DEC-21 | IMPL-058 | `metadata_only` members are skipped (critic 25) |
> | IMPL-DEC-4 | IMPL-025 | Per-answer budget check at close (critic 28) |
> | Open questions 1–4 | OWNER-CHECKPOINT.md §c | Merged with the contracts analyst's questions |


Role: decision-engine architect. Gaps owned: **1, 2, 5, 25, 27, 28** (from `.orchestration/context/01-gaps.md`), plus the new gaps found while specifying (marked `gap new`).
Spec: [`../impl-spec/06-decision-engine.md`](../impl-spec/06-decision-engine.md). ADRs: [ADR-003](../adr/ADR-003-calibrator-scope.md), [ADR-004](../adr/ADR-004-conditioning-placement.md).
Ref keys: `02`/`03`/`04`/`07`/`08` = docs/noema/0x-*.md, `types` = contracts/types.ts, `schema` = contracts/decision.schema.json, `ce` = contracts/check_examples.py, `yaml` = contracts/examples/platform.example.yaml.
**Material** = changes externally visible API behavior, adds API surface, or touches an owner decision. Contract patches are *proposed* here and applied after owner review.

## Summary

| Id | Gap | Title | Material | Patch |
|---|---|---|---|---|
| IMPL-DEC-1 | 1 | Status at deadline with quorum met = `completed`; `partial` = any answer unavailable | no | doc + contract comment |
| IMPL-DEC-2 | 2 | `quorum` = non-final in-turn snapshot; ≤ 1 `decision.updated` | **yes** | doc + contract comment |
| IMPL-DEC-3 | 5 | Keep `any_refused` (config) → `refused` (triggered_by) mapping | no | doc |
| IMPL-DEC-4 | 25 | Keep deadlines; in-turn escalation only within the decision wait limit (never with defaults); targets inherit source questions | **yes** | doc |
| IMPL-DEC-5 | 27 | Hierarchical, early commit, batch, sampling, log/stacked pool: not in Phase 1 (422 / config reject) | **yes** (R5) | doc |
| IMPL-DEC-6 | 28 | `ordinal_dispersion` per types.ts (mode, ties → lowest index, MAD about the centre) | no | doc |
| IMPL-DEC-7 | new | Response answer names may be `q<digits>` (facade internal names) | **yes** | contract (CP-DEC-1) |
| IMPL-DEC-8 | new | Escalation stages record their own view signature and templates | **yes** | contract (CP-DEC-2) |
| IMPL-DEC-9 | new | Profile resolution chain and errors (403 / 422 / 400 facade) | **yes** | doc |
| IMPL-DEC-10 | new | Jev/facade edge mapping: Jev argmax on rounded values; unresolved mass the format can't carry → unavailable/refusal | **yes** | doc |
| IMPL-DEC-11 | new | No member retries; time-triggered backups only, first valid answer wins | no | none |
| IMPL-DEC-12 | new | Option order: only choice options are permuted; seed from the decision id | no | doc |
| IMPL-DEC-13 | new | `/v1/decisions` `session_id`: access-checked, no history in Phase 1 | no | none |
| IMPL-DEC-14 | new | Exact decision templates, JSON and tag escaping, standard spotlighting, provider-signalled refusals | no | none |
| IMPL-DEC-15 | new | No automatic `textFormat` fallback in Phase 1 (alert only) | no | none |
| IMPL-DEC-16 | new | Usage lines: completed calls → `decision`/`verification`; cancelled → `waste` | no | none |
| IMPL-DEC-17 | new | `include` semantics; stored row always full | no | none |
| IMPL-DEC-18 | new | Facade remote image URL = HTTP 400 `unsupported_parameter`; > 20 images = 400 `schema_violation` | no | doc |
| IMPL-DEC-19 | new | Decision row written synchronously in `finalize()`; native all-unavailable → 200 `partial` | no | none |
| IMPL-DEC-20 | new | L2 key part contents, put/get gates, hit semantics | no | doc |
| IMPL-DEC-21 | new | Quorum counting and the classification order | no | none |
| IMPL-DEC-22 | new | Input digest canonicalization and per-tenant HMAC key | no | none |
| IMPL-DEC-23 | new | Largest-remainder 4-dp rounding; ordering on emitted values | no | none |
| IMPL-DEC-24 | new | Final answer = last stage's answer, even if `unavailable` | no | none |

---

## IMPL-DEC-1 (gap #1): Decision status when the deadline passes with quorum met

**Decision.** `types.ts` wins:
- `completed` means every answer is final and none is `unavailable`. That includes the case where the deadline passed with quorum met; the members that missed the deadline are recorded as `timeout`.
- `partial` means every answer is final and at least one is `unavailable`, for **any** reason (deadline, no quorum, all failed, no capable member), not only the deadline.
- `04:L110` is corrected to match.

**Rationale.**
- `types` and `schema` are the machine-checked contract.
- An answer that met quorum is a complete answer, so calling the decision `partial` would make clients treat good answers as degraded.
- `partial` keyed to "has an `unavailable` answer" gives clients one boolean to branch on.

**Refs.** `types:L397-402`, `04:L110`, `schema` `DecisionResult.status`, spec §7.4, worked example T-POOL-2.

**Patch needed: doc + contract comment.**
- `04:L110`, replace the "Before commit" cell with: "answers that meet quorum are served from the members that answered (missing members `timeout`; decision `status: completed`); answers below quorum are `unavailable` (decision `status: partial`) → bindings use defaults (`default_profile`, no conditioning)".
- `types:L397-401` comment: replaced by the IMPL-DEC-2 text.

## IMPL-DEC-2 (gap #2): `quorum` status while early commit is off

**Decision.**
- `status: "quorum"` marks the **in-turn snapshot** returned by `DecisionRun.commit_critical()` while at least one non-blocking answer is still open.
  - In that snapshot, every commit-critical answer is final: closed when all required members are terminal, when quorum became impossible, or at the deadline.
  - The still-open answers are provisional: computed from the members that have answered, with pending members `late`.
- Exactly one `decision.updated` follows, carrying the final result (`completed`/`partial`). Its `changed` lists the names that became final.
- If every answer is final at commit-critical time, the snapshot is already `completed`/`partial` and no `decision.updated` follows.
- `/v1/decisions`, the facade and the stored row never carry `quorum`.
- **This is not early commit.** The commit still waits for the slowest required member of every commit-critical question, or the deadline. R5 ("early commit OFF") is unchanged, and the certification rule of 02 §6.3 is not implemented (IMPL-DEC-5).

**Rationale.**
- Only commit-critical questions may block the commit (`02:L214`). The non-blocking predicates are asked of the same members in parallel and can finish later.
- Waiting for them would block the commit. Emitting `decision.completed` late would reorder the documented stream (`07:L313-327`: `decision.completed` before `route.confirmed`).
- The existing enum value and its `types` definition ("returned early at quorum inside a turn; late members may follow as `decision.updated`") already describe this snapshot. It also gives Phase 1 meaning to `decision.updated` and to the `late` status (`03:L207`).
- The 03 example keeps `status: "quorum"`. Its `decision.updated` changes a commit-critical answer (`complexity`), which only early commit could do, so that example is corrected.

**Refs.** `02:L214`, `02:L516`, `02:L540`, `03:L156`, `03:L176`, `03:L207`, `07:L317`, `types:L397-402`, spec §7.4 and §10, test T-RUN-1.

**Patch needed: doc + contract comment.**
- `types.ts` L397-401 (comment only; the enum is unchanged):
  ```ts
  /**
   * completed: every answer is final and none is `unavailable` (every required member answered, or the deadline passed with quorum met).
   * quorum:    turn snapshot sent when every commit-critical answer is final but a non-blocking answer is still open; open answers are
   *            provisional (pending members `late`). The final result follows as one `decision.updated`. Never returned by /v1/decisions.
   * partial:   every answer is final and at least one is `unavailable`.
   */
  ```
- `03:L176`: `"changed":["complexity"]` → `"changed":["sensitive_data"]`.
- `07:L317`: `the decision (status "quorum" or "completed")` → `the decision: "completed" or "partial", or "quorum" while non-blocking answers are still pending (one decision.updated then follows)`.
- `02` §6.3, after L540, add: "With early commit off, `quorum` also marks the in-turn snapshot sent when every commit-critical answer is final but a non-blocking answer is still pending; its commit-critical answers never change afterwards."

## IMPL-DEC-3 (gap #5): Escalation trigger names

**Decision.** Neither contract is renamed. The config key `escalation.when.any_refused` is recorded in `EscalationStage.triggered_by` as `refused`. The other keys map one-to-one.

| Config key | Fires when | `triggered_by` |
|---|---|---|
| `confidence_below` | scored and `confidence.value < x` | `confidence_below` |
| `margin_below` | scored and `confidence.margin < x` | `margin_below` |
| `any_refused: true` | `quorum.refused ≥ 1` (every `refusal` answer qualifies) | `refused` |
| `unavailable: true` | answer type `unavailable` | `unavailable` |

`triggered_by` is the union over the stage's escalated questions, in the order above.

**Rationale.** Both names are validated by machine-checked contracts (the config schema and `schema` `EscalationStage`), and `check_config` has negative tests on the config key. A mapping costs one table. A rename would touch two contracts and the audit.

**Refs.** `08:L96`, `yaml:L127`, `types:L283`, spec §9.1.

**Patch needed: doc.** `08:L96`, append to the Notes cell: "`EscalationStage.triggered_by` records the fired keys as `confidence_below`, `margin_below`, `refused` (from `any_refused`) and `unavailable`."

## IMPL-DEC-4 (gap #25): Latency vs deadlines; how `router-plus` gets its questions

**Decision.**
1. **Haiku vs the 1,000 ms deadline.** The deadline stays at its design value (PLAN rule).
   - A Haiku call still running at the deadline is `timeout`.
   - The answer from {DeepSeek, Luna} meets `min_quorum: 2`, so the decision is `completed` and the answer is `uncalibrated` (no artifact for that subset).
   - The wire-mock latency profiles must include Haiku > 1 s so this path is exercised.
   - Phase 0 task 0.4 re-sets the deadline, quorum and `commit_critical` from data.
2. **Escalation inside turns.**
   - An escalation stage starts only if `now + target.deadline_ms ≤ min(ctx.escalate_until, ctx.deadline)`.
   - The orchestrator sets `escalate_until = decision_start + router.deadline_ms + speculation.slack_ms`: the latest time it waits for the decision before evaluating the bindings.
   - With the defaults (2,500 ms > 1,050 ms), escalation **never runs inside turns**. A low-confidence `intent` flows into the bindings (`cover_top2_and_clarify`), as `02:L423` allows, and the `max_hold_ms` conflict can't arise.
   - On `/v1/decisions` and the facade, `escalate_until = ctx.deadline` (end-to-end), so escalation runs normally.
3. **Question inheritance.**
   - The target re-asks the **source stage's compiled questions** (same `CompiledQuestion`, same signatures). It uses its own members, templates, efforts, weights, `min_quorum`, `deadline_ms`, aggregation and `modality_policy`.
   - The target's own `questions` and `commit_critical` are ignored when it runs as an escalation target, which is why `router-plus` needs no `questions`.
   - Request overrides (`deadline_ms`, `min_quorum`) apply to stage 0 only.
   - Chains continue through the target's own `escalation` (cycles are config-rejected).

**Rationale.**
- The numeric parameters are frozen until Phase 0.
- Escalating after the commit would cost three calls per question and could only affect the next turn's sticky spec. That's a feature nobody asked for.
- "Time allows" needs one testable inequality.
- Inheriting compiled questions keeps `question_signature` stable across stages, which the stage-bound calibrator binding needs.

**Refs.** `02:L207`, `02:L215`, `02:L295`, `02:L421-424`, `04:L110`, `yaml:L104-127`, spec §9.3, tests T-POOL-2 and T-ESC-3.

**Material: yes.** Turns get no escalation under the shipped configuration. The owner should see this.

**Patch needed: doc.**
- `02:L423` → "**In turns**, escalation only runs if the target stage fits before the orchestrator's decision wait limit (decision start + router `deadline_ms` + `slack_ms`); with the default `router-plus` (2,500 ms) it never does. Otherwise the low-confidence answer flows into the bindings (§6.5), which still use its ambiguity through conditioning."
- `08:L96`, append: "The target re-asks the source stage's questions with its own members, deadline and quorum; its own `questions` and `commit_critical` are ignored."

## IMPL-DEC-5 (gap #27): Hierarchical method and early commit in Phase 1

**Decision.** Neither is implemented in Phase 1. The same applies to every other out-of-phase decision mechanism.

| Mechanism | Phase 1 |
|---|---|
| Hierarchical method | A question with `K > L(profile)` (L = the smallest member template `max_choices`) gets 422 `unsupported_parameter`, `param: questions[i].choices`, `details: {max_choices: L, method: "hierarchical"}`. This applies on native, Jev, the facade and turn app questions alike, and follows the gap-36 out-of-phase error shape if that adds fields. Platform questions above `L` in `routing.router_profile` or `routing.verification.decision_profile` are config-rejected (P1-D6). `group` is accepted and signed, but unused. An escalation target that would need the method is skipped for that question. |
| Early commit | `routing.speculation.commit_on_quorum: true` is config-rejected (P1-D1). No certification code exists. |
| `batch_questions: true` | config reject (P1-D2) |
| `sampling`, `native_decision` | config reject (P1-D3) |
| `log_pool`, `stacked` | config reject (P1-D4) |
| templates other than `logprob-codes@2`, `verbalized-dist@1`, `verify-dist@1` | config reject (P1-D5) |

The P1 rules live in `config/rules.py` (foundation), each with a negative test.

**Rationale.**
- 05 L75 lists neither mechanism in Phase 1, and both are [X].
- A typed 422 is honest and testable. A half-built hierarchical path would emit `unresolved_mass` that the calibration and validation chain has never exercised.
- The 255-choice API limit stays in the schema, so lifting the 422 later is additive.

**Refs.** `05:L75`, `02:L312-318`, `08:L243-295`, `03:L220`, `03:L297`, `07:L154-159`, spec §13.

**Material: yes. This touches R5** ("255-choice limit with hierarchical method [X]"). Native requests above the flat limit, which the docs say are answered hierarchically, get a 422 in Phase 1. Owner confirmation is requested (open question 1).

**Patch needed: doc.** Add to `03` §4.1 (after L220), `07:L154` (sub-bullet) and `08` §3.4 (after L254): "Phase 1: the hierarchical method is not available yet. A question with more options than the profile's smallest `max_choices` gets 422 `unsupported_parameter` (`param: questions[i].choices`, `details.max_choices`)."

## IMPL-DEC-6 (gap #28): `ordinal_dispersion` formula

**Decision.** Follow `types:L185` and `ce:L49-54` exactly:
- `m = argmax_i p_i`, with ties to the **lowest** index;
- `MAD_u = (1/K)·Σ_i |i − (K−1)/2|`, the uniform distribution's mean absolute deviation about its centre;
- `value = max(0, 1 − Σ_i p_i·|i − m| / MAD_u)`.

**Rationale.** It is the machine-checked formula. It reproduces the OpenAI guide value (0.1, 0.7, 0.2) → 0.55 and the mode-vs-median self-test (0, .3, .3, .4, 0) → 0.25. For K = 2 it equals `normalized_max`.

**Refs.** `02:L412`, `types:L185`, `ce:L49-67`, spec §11, test T-OD-1.

**Patch needed: doc.** `02:L412`: replace "`ordinal_dispersion = max(0, 1 − Σ p_i·|i − m| / MAD_uniform)`, with m = **most likely level** [TS-C]" with "`ordinal_dispersion = max(0, 1 − Σ p_i·|i − m| / MAD_u)`, with m = **most likely level** (ties → lower index) and `MAD_u = (1/K)·Σ_i |i − (K−1)/2|` (the uniform distribution about its centre) [TS-C]".

## IMPL-DEC-7 (gap new): Response answer names for facade decisions

**Decision.**
- The facade names each question internally:
  - an explicit name that matches the native name pattern is kept;
  - any other question is named `q{i}`, by 0-based position. That covers unnamed questions and explicit names the native pattern rejects (e.g. `"Severity Level"`).
- The stored native result, which `GET /v1/decisions/{x_aghoo.decision_id}` returns, uses these names.
- `schema` `Name` rejects `^q[0-9]+$` and is also used for **answer** names, so such a result would be schema-invalid. Proposed fix: add a response-side `AnswerName` without the reservation (CP-DEC-1). Requests keep `Name`, so a client still can't send `q0`.

**Rationale.** The reservation exists so generated names can't collide with explicit ones (`03:L295`). It only needs to apply to requests. Without the patch, every unnamed facade question produces a stored decision that fails the server's own response validation.

**Refs.** `schema` `$defs.Name` (L8-12), `03:L295`, `openai-compat.schema.json` `OAQuestion.name` (no pattern), spec §12.3.

**Patch needed: contract (CP-DEC-1, below).**

## IMPL-DEC-8 (gap new): Per-stage provenance for escalated answers

**Decision.** `EscalationStage` gains two optional, additive fields, which the server always sets:
- `input_view_signature`: the view signature of that stage's members;
- `prompt_templates`: `member_id → template` for that stage.

A calibrator binding for an escalated answer uses its stage's view signature. `check_examples` reads the answering stage's signature and templates, falling back to `provenance` for stage 0 (CP-DEC-2).

**Rationale.**
- Member ids are positional per stage (`m1`…). With `lightweight-model-decide` (m1 = Luna, verbalized) → `router-default` (m1 = DeepSeek, logprob), `provenance.prompt_templates["m1"]` points at the wrong template for the escalated answer's m1.
- `provenance.input_view_signature` ("m1:native") can never equal the binding of a `router-default` answer ("m1:…,m2:…,m3:…"). Calibrated escalated answers would therefore always fail invariant 5, even when correct.
- The shipped escalated example hides this, because router-default and router-plus share members and views.

**If the owner rejects CP-DEC-2:** escalated answers skip the calibrator lookup and are always `uncalibrated` in Phase 1, and `prompt_templates` stays stage-0 only.

**Refs.** `ce:L151-157`, `ce:L183-201`, `ce:L320-330`, `types:L280-286`, `examples/decision-result.valid-escalated.json`, spec §3.5 and §9.3.

**Patch needed: contract (CP-DEC-2, below).**

## IMPL-DEC-9 (gap new): Profile resolution and its errors

**Decision.**
- Resolution chain: request `profile` (or facade `model`) → `tenants.<t>.defaults.decision_profile` → platform constant `lightweight-model-decide`. Steps 2–3 apply only if gap 31 makes `profile` optional; this is my working assumption.
- Allowlist: `{default} ∪ request_overrides.decision_profiles`.

| Case | Native | Facade |
|---|---|---|
| unknown id, or outside the allowlist | 403 `profile_not_allowed` (not distinguished, so the config can't be enumerated) | same |
| pin `@v` ≠ the active version | 422 `unsupported_parameter`, `param: profile`, `details {requested, active}` | 400 `model_not_available` |
| `model` not of the form `aghoo/…` | — | 400 `model_not_available`, `details.allowed` |
| `verify-dist` profile with a standard input | 422 `unsupported_parameter` | same |
| unresolvable default | 400 `schema_violation`, `param: profile` | — |

On `/v1/decisions` and the facade, a profile's built-in `questions` and `commit_critical` are ignored.

**Rationale.** The config holds one active version per profile id. Serving old versions would need a versioned registry, which is out of Phase 1 scope. The error codes all exist in the 03 §1 table.

**Refs.** `03:L30`, `03:L221`, `03:L303`, `08:L33-34`, spec §2.

**Material: yes** (defines visible errors). **Patch needed: doc.** Add to `03` §4.1: "A version pin must name the active version (otherwise 422 `unsupported_parameter`; on the facade 400 `model_not_available`); unknown and non-allowlisted profiles are 403 `profile_not_allowed`."

## IMPL-DEC-10 (gap new): Jev and facade mapping edge cases

**Decision.**
1. Jev `choice` is the argmax over the **2-dp** probabilities, ties in declared order, as Jev invariant 3 requires (`ce:L90`). It can differ from the native `choice` only when two candidates round to the same 2-dp value.
2. A Jev choice or score answer whose rounded map would violate `|Σ − 1| ≤ max(0.011, 0.005·K)` becomes `{type: "unavailable", reason: "no_capable_member"}`. A facade answer with emitted `unresolved_mass > 0.001` becomes `refusal`, with `x_aghoo.mapped_unavailable[name] = "no_capable_member"`.
   - Only a large censored logprob tail can trigger either rule; on realistic inputs that mass is ≪ 0.001 (T-LP-1).
   - Neither format has an `unresolved_mass` field (`02:L318`), and the mass must never be spread over options.

**Rationale.** The response must satisfy the documented invariants without renormalizing. The format-level 422 used for the hierarchical method can't apply, because censoring is only known after the call.

**Refs.** `03:L375-377`, `08:L236-241`, `08:L274-280`, `ce:L83-108`, `ce:L418-431`, spec §12.2–12.3.

**Material: yes** (a visible answer type in a rare case). **Patch needed: doc.** Add one sentence each to `03` §4.3 and §4.2 stating these rules.

## IMPL-DEC-11 (gap new): Member calls, retries and backups

**Decision.**
- Decision member calls are **never retried**.
- A backup is purely time-triggered. At `backup_after_ms`, if the primary is still in flight, an identical call is sent. A backup applies only when `backup_after_ms + latency_p95_ms ≤ the effective stage deadline_ms`.
- The first call to finish `ok` or `refused` wins, and the other is cancelled. An `error` waits for the other call. A primary error before `backup_after_ms` is final.
- Backups don't consume `RetryBudget`, but admission reserves them.
- A circuit-open model or a disallowed model gives `error` without a call.

**Rationale.** The 10% retry budget (`04:L121`) would be exhausted by a 400 ms backup that "hedges most DeepSeek calls" (`02:L216`). Error retries inside a 1 s deadline add load without much gain. This gives a simple, deterministic rule.

**Refs.** `02:L216`, `04:L111`, `04:L119-123`, `08:L95`, spec §6.3. **Patch needed: none.**

## IMPL-DEC-12 (gap new): Option order permutation and seed

**Decision.**
- Only **choice** options are permuted, per (stage profile@version, member, question).
- Predicates are fixed: `A = true`, `B = false` (`08:L199`).
- Score levels keep declared order, with `A` the lowest, which keeps the ordinal meaning.
- `option_order_seed` = the first 4 bytes of `sha256(decision_id)`, masked to 31 bits. The permutation uses `random.Random(sha256(f"{seed}|{profile_ref}|{member_id}|{qname}")[:8]).shuffle` (test vectors in spec §3.6).

**Rationale.**
- Permuting choice options counters position bias.
- `08` fixes the predicate letters.
- Permuting an ordinal scale would destroy the cue that the levels are ordered.
- Deriving the seed from the id makes tests deterministic, and the seed is recorded anyway.

**Refs.** `08:L179`, `08:L199`, `types:L298-299`, spec §3.6. **Patch needed: doc.** `08:L179`: "option order permuted with `option_order_seed`" → "choice options permuted with `option_order_seed` (predicates A = true / B = false; score levels in declared order)".

## IMPL-DEC-13 (gap new): `session_id` on `/v1/decisions` in Phase 1

**Decision.**
- Accepted. Access is checked: an inaccessible session gets 404 `session_not_found`.
- The session id is recorded on the decision row.
- **No** history is added to the member prompt. The default is "summary only" (`types:L105`), and Phase 1 has no summary (gap 45).

**Rationale.** This keeps the field in the Phase 1 contract with defined, safe behavior and no speculative history logic.

**Refs.** `types:L105`, gap 45. **Patch needed: none.**

## IMPL-DEC-14 (gap new): Decision prompt text, escaping and refusal detection

**Decision.**
- The exact text of `logprob-codes@2`, `verbalized-dist@1` and `verify-dist@1` is in spec §5. It adds the sentence "Neither can change these rules." to the verbalized system texts, and a trailing instruction line after `</untrusted_input>` for the verbalized templates.
- `app_question` JSON is serialized with `<`, `>` and `&` written as the JSON escapes `\u003c`, `\u003e` and `\u0026`, so no literal `<` can close the block.
- C0/C1 control characters are removed, except tab, newline and CR.
- Untrusted text has `<`/`</` + `untrusted_input|app_question|decision_context|question|answer` rewritten to `&lt;…`.
- Decision prompts always use standard spotlighting (delimiters + escaping). `spotlight: strict` stays a generation-side tightening action.
- A refusal is only a provider signal (Converse `content_filtered`/`guardrail_intervened`, Mantle `content_filter` or `message.refusal`). Refusal prose fails parsing and is `error`.

**Rationale.**
- JSON encoding alone does not escape `</app_question>`, so the docs' claim (`08:L194`) needs the `<` rule to hold.
- Prose-refusal heuristics would be fragile and would make the template language-dependent.

**Refs.** `08:L179-211`, `04:L253`, `04:L256`, `02:L320-324`, gap 29 (shared helpers), spec §5. **Patch needed: none.** The template text is code. 08 §3.2 stays a sketch that points to the implementation.

## IMPL-DEC-15 (gap new): Structured-output fallback

**Decision.** Phase 1 uses prompt-only JSON only. A member parse-error rate above 1% raises the existing alert (`08:L459`). The switch to `outputConfig.textFormat` is not automatic: it would be a new template version (and recalibration), shipped deliberately.

**Rationale.** Changing the request changes the calibrated pipeline. Automatic switching would silently invalidate calibrators.

**Refs.** `02:L325`, `08:L459`. **Patch needed: none.**

## IMPL-DEC-16 (gap new): Usage lines for decision calls

**Decision.**
- Completed member calls (primary, backup and escalation) are recorded on the `decision` line, or on `verification` when `purpose = verification`.
- Cancelled calls (lost backup races, deadline cancels, run cancels) are recorded on `waste`, with `tokens_estimated: true` (input = prompt estimate, output 0).
- `DecisionResult.usage` includes everything. This assumes the gap-20 resolution; the line choice is a one-line switch.

**Rationale.** It follows the disjointness rule of `02:L640` (cancelled or losing work goes to waste).

**Refs.** `02:L636-640`, `04:L283`, gap 20. **Patch needed: none.**

## IMPL-DEC-17 (gap new): `include` semantics

**Decision.**
- Without `include`, `/v1/decisions` answers omit `members`.
- `members` adds `members[]` (with distributions, which the schema requires for `ok`).
- `member_distributions` implies `members` and adds `calibrated_distribution` and `calibrator_id` where an artifact applied.
- `stages` is a no-op, because `provenance.stages` is always present (`ce:L382-383`).
- The stored row and `GET /v1/decisions/{id}` are always full.
- Turn decisions always include members, for the inspector.

**Refs.** `types:L107`, `schema` `MemberVote` `allOf`, `03:L380`. **Patch needed: none.**

## IMPL-DEC-18 (gap new): Facade request-shape errors

**Decision.**
- A remote `image_url` gets HTTP **400**, `invalid_request_error`, code `unsupported_parameter` (`03:L300`), even though `03:L56` lists `unsupported_parameter` under 422 `capability_error`. The code is the same, and the status follows the specific rule.
- More than 20 images gets 400 `schema_violation`, `param: input`, `details.max_images: 20`.

**Refs.** `03:L56`, `03:L299-300`, gap 32 (contracts owner adds the facade error responses). **Patch needed: doc.** `03:L56`: add "(`unsupported_parameter` is also returned as 400 for request-shape cases such as a remote image URL on the facade)".

## IMPL-DEC-19 (gap new): Persistence and the native all-unavailable case

**Decision.**
- The decision row is written synchronously by `DecisionRun.finalize()`: one `INSERT` under `uow(principal)`. `store: false` turns persist nothing.
- On native `/v1/decisions`, a result where every answer is `unavailable` returns 200 with `status: "partial"`.
- 503 `tenant_access_misconfigured` is returned only when credentials fail at `start()`, or when every answer is unavailable and every member error is `tenant_access_misconfigured`. The decision is still stored, and `details.decision_id` is set.
- The facade returns 503 `all_routes_unavailable` (`03:L313`).

**Rationale.**
- A `GET` right after a create must find the row, which an outbox would not guarantee. This interacts with gap 22, whose owner covers turns.
- Clients branch on answer types (`07:L197-199`).

**Refs.** `03:L313`, `04:L104`, `07:L197`. **Patch needed: none.**

## IMPL-DEC-20 (gap new): L2 key parts and hit semantics

**Decision.** The key has 8 parts (assuming gap 4 resolves to `04:L147`):
1. tenant (key prefix);
2. `project_id`;
3. question-set signature;
4. `input_digest`;
5. stage-0 `input_view_signature`;
6. `"{id}@{ver};q={min_quorum};d={deadline_ms};e={off | escalation chain id@ver list}"`, so request overrides and chain versions are part of the "profile version";
7. `cal:{CalibratorRegistry.epoch(tenant)}`, standing in for "calibrator_id", because the calibrator depends on the answering subset, which is only known after the call;
8. `policy:{policy_version}`.

Gates and behavior:
- Get/put only when `store` is true, the data class is public or internal, and the purpose is not `verification`.
- Put only `completed` results that have no `metadata_only` view.
- TTL 600 s.
- A hit gets a new `dec_` id, `cache.hit: true`, zero usage, and the fill-time `started_at`/`completed_at` (so invariant 8 holds). The hit is persisted as a new row.

**Rationale.** These are the minimum parts that make a hit observationally equal to a fresh computation. The `metadata_only` and `partial` exclusions stop transient failures from being cached.

**Refs.** `04:L147`, `ce:L378-381`, spec §14. **Patch needed: doc.** `04:L147`: add "the profile part includes the request overrides and the escalation chain; the calibrator part is the tenant's calibrator epoch; only `completed` results without metadata-only views are stored".

## IMPL-DEC-21 (gap new): Quorum counting and classification order

**Decision.**
- Quorum counts **distinct models** among `ok`, non-optional members with `weight > 0` (`08:L129`).
- Optional `ok` members join the pool but don't count toward quorum. `quorum.responded` counts every `ok` member (`ce:L346`).
- An answer closes when every required member is terminal, when quorum becomes impossible, at the stage deadline, or on cancel.
- Classification order: scored → no_capable_member (adequacy) → refusal → no_capable_member (skips) → all_members_failed → deadline_exceeded → no_quorum (spec §7.3).

**Refs.** `02:L362`, `08:L95`, `08:L129`, `08:L283-284`, spec §7. **Patch needed: none.**

## IMPL-DEC-22 (gap new): Input digest

**Decision.**
- The per-tenant key is `K_t = HMAC(pepper, "noema/input-digest/v1/" + tenant_id)`. The pepper is a platform secret from `SecretsProvider`.
- The digest is `HMAC(K_t, canonical input)`:
  - strings byte-exact (this matches the example);
  - everything else as canonical JSON with `ensure_ascii=False`;
  - inline images as their sha256;
  - file parts as the file sha256 plus the sorted `kind:sha256` of every ready derivative.

**Rationale.**
- Per-tenant keys make digests incomparable across tenants.
- Including the derivative hashes makes "never serve a metadata-only decision after derivatives are ready" automatic, and it tracks extractor-version changes.

**Refs.** `types:L294`, `04:L155`, `mk:L219`, spec §3.4. **Patch needed: none.**

## IMPL-DEC-23 (gap new): Rounding and ordering

**Decision.**
- Candidates and member distributions are emitted at 4 dp using largest-remainder rounding (spec §8.2), so they sum exactly to `1 − unresolved_mass`.
- `raw_score` uses a plain round.
- Ordering, `choice`, `level` and votes use the emitted values, with ties in declared order.
- Confidence fields are computed on unrounded values (`ce` allows 2e-3).
- The band uses the emitted values (as `ce` does).

**Rationale.** `make_examples` adds the rounding residual to the top candidate, which can reorder candidates in near-ties. Largest-remainder rounding never does, and it reproduces every shipped example (T-POOL-1, T-ESC-2).

**Refs.** `mk:L90-91`, `ce:L274`, `ce:L287`, `ce:L314`, spec §8.2. **Patch needed: none.**

## IMPL-DEC-24 (gap new): Which stage's answer is final

**Decision.** A question's final answer is the answer of the **last stage that asked it** (`02:L422`), even if that stage returned `unavailable`. For example, `router-plus` (quorum 3) can time out on Haiku and turn a scored stage-0 answer into `unavailable`. The design is applied literally; Phase 0 (06 §2.2 "Escalation") measures how often that happens.

**Rationale.** It keeps the calibrator stage rule and `ce` `answering_stage` consistent: the answer always belongs to the last stage listing it. **Refs.** `02:L422`, `ce:L151-157`. **Patch needed: none.** Open question 3 asks the owner.

---

## Proposed contract patches

### CP-DEC-1: response answer names (IMPL-DEC-7), `contracts/decision.schema.json`

```diff
     "Name": {
       "type": "string",
       "pattern": "^(?!q[0-9]+$)[a-z][a-z0-9_]{0,63}$",
-      "description": "q followed by digits is reserved: the OpenAI facade names unnamed questions q0, q1, … internally."
+      "description": "Request-side question name. q followed by digits is reserved: the OpenAI facade names unnamed questions q0, q1, … internally."
     },
+    "AnswerName": {
+      "type": "string",
+      "pattern": "^[a-z][a-z0-9_]{0,63}$",
+      "description": "Response-side answer name: a request Name, or a facade-generated q<digits> name (stored facade decisions)."
+    },
```

Then change `"$ref": "#/$defs/Name"` to `"$ref": "#/$defs/AnswerName"` in exactly these four places: `ScoredBase.properties.name`, `RefusalAnswer.properties.name`, `UnavailableAnswer.properties.name` and `EscalationStage.properties.questions.items`. The `*Question.name` fields and `JevDecisionResponse.answers.propertyNames` keep `Name`.

- `types.ts`: no type change (names are `string`). Add one comment on `ScoredAnswerBase.name`: `/** Request name, or q<digits> for unnamed facade questions (stored facade decisions). */`.
- `check_examples.py` and `python_sdk_types.py`: no change.
- Test: a schema case (a `DecisionResult` with an answer named `q0` validates, while a `DecisionCreateParams` question named `q0` is still rejected). The existing audit case at `qa/run_design_audit.py:L367` keeps covering the request side. A paired example file is not added, because `check_examples` pairs results with native requests, which cannot carry `q0`.

### CP-DEC-2: per-stage provenance (IMPL-DEC-8)

`contracts/decision.schema.json`, `EscalationStage.properties` (additive; `required` unchanged):

```diff
         "questions": {
           "type": "array",
           "items": {
-            "$ref": "#/$defs/Name"
+            "$ref": "#/$defs/AnswerName"
           }
         },
         "usage": {
           "$ref": "#/$defs/Usage"
-        }
+        },
+        "input_view_signature": {
+          "type": "string",
+          "pattern": "^sha256:[0-9a-f]{64}$",
+          "description": "sha256 over this stage's per-member input views (members are numbered m1..mN within the stage). Calibrator bindings of answers from this stage use it."
+        },
+        "prompt_templates": {
+          "type": "object",
+          "additionalProperties": {
+            "type": "string",
+            "pattern": "^[a-z0-9-]+@[0-9]+$"
+          },
+          "description": "member_id -> template version for this stage's members."
+        }
       }
     },
```

(The `AnswerName` line is CP-DEC-1, shown here in context.)

`contracts/types.ts`, `EscalationStage`:

```diff
   questions: string[];
   usage: Usage;
+  /** sha256 over this stage's per-member input views; calibrator bindings of answers from this stage use it. */
+  input_view_signature?: string;
+  /** member_id -> template version for this stage's members (member ids are positional within the stage). */
+  prompt_templates?: Record<string, string>;
 }
```

`contracts/check_examples.py`:

```diff
 def answering_stage(result, name):
-    """(profile_id, profile_version) of the last stage that answered this question: provenance, or the last escalation."""
+    """The last stage that answered this question (the last escalation listing it, else provenance):
+    (profile_id, profile_version, input_view_signature, prompt_templates)."""
     p = result["provenance"]
     for stage in reversed(p["stages"]):
         if name in stage["questions"]:
-            return stage["profile_id"], stage["profile_version"]
-    return p["profile_id"], p["profile_version"]
+            return (stage["profile_id"], stage["profile_version"],
+                    stage.get("input_view_signature", p["input_view_signature"]), stage.get("prompt_templates", p["prompt_templates"]))
+    return p["profile_id"], p["profile_version"], p["input_view_signature"], p["prompt_templates"]
@@ def template_limit_errors(a, k, result, method):
     for m in a.get("members", []):
-        tpl = result["provenance"]["prompt_templates"].get(m["member_id"])
+        tpl = answering_stage(result, a["name"])[3].get(m["member_id"])
@@ def result_invariants(result, req):
-            if (b["profile_id"], b["profile_version"]) != stage:
+            if (b["profile_id"], b["profile_version"]) != stage[:2]:
@@
-            if b["input_view_signature"] != result["provenance"]["input_view_signature"]:
+            if b["input_view_signature"] != stage[2]:
```

`make_examples.py`: the escalated example adds `"input_view_signature": sha(...)` and `"prompt_templates": {...}` to `stages[0]`, with the same values as today's provenance. Then regenerate.

`python_sdk_types.py`: no change (`stages: list[dict]`).

Audit: add one case, an escalated `lightweight-model-decide → router-default` result calibrated with a router-default binding and the stage's view signature. It must be accepted, and it must be rejected when the stage-0 signature is used.

**Verified on a scratch copy** (`/tmp`, docs/noema untouched), with CP-DEC-1 and CP-DEC-2 applied:
- `check_examples.py`: 0 unexpected outcomes on all 43 examples.
- `qa/run_design_audit.py`: 41/41 handled.
- A `q0` answer validates as a `DecisionResult`, while a `q0` request question is still rejected.
- The escalated stage-bound calibrated result above passes `result_invariants`, and the same result with the stage-0 view signature fails.
- The **unpatched** checker rejects the correct stage-bound result (`binding input_view_signature != the views this decision used`), which confirms the gap.

## Open questions for the owner

1. **R5 / hierarchical (IMPL-DEC-5).** Phase 1 returns 422 `unsupported_parameter` for choice questions above the profile's flat limit: 16 options on `router-default`, 26 on `lightweight-model-decide`. Do you accept deferring the hierarchical method (the API limit stays 255), or must Phase 1 implement it (about one extra engineer-week plus calibration plumbing)?
2. **Turn escalation (IMPL-DEC-4).** With the shipped numbers, escalation never runs inside turns: `router-plus` needs 2,500 ms against a 1,050 ms wait limit. Accept? The alternative is a post-commit escalation that only feeds `decision.updated` and the next turn's sticky spec, at three extra calls per escalated question.
3. **Final-stage rule (IMPL-DEC-24).** Should an escalation stage that returns `unavailable` (e.g. a Haiku timeout under `router-plus` quorum 3) replace a scored stage-0 answer, as the design says, or should the earlier scored answer be kept?
4. **CP-DEC-2 (IMPL-DEC-8).** Accept the two additive `EscalationStage` fields? If not, escalated answers are always served `uncalibrated`.

## Notes and dependencies on other roles

| Other gap / role | What this role assumes |
|---|---|
| gap 31 (contracts) | `profile` becomes optional with the IMPL-DEC-9 chain. If it stays required, steps 2–3 are unreachable on native; nothing else changes. |
| gap 23 (deadline derivation) | The orchestrator passes `ctx.deadline` (end-to-end) and `ctx.escalate_until = decision_start + router.deadline_ms + slack_ms`. |
| gap 4 (L2 key) | 04's 8-part key. |
| gap 6 (span names) | 08 §7 names (`decision`, `decision.member`, `decision.escalation`). |
| gap 12 (SSE order) | `decision.completed` comes from `commit_critical()` before the bindings; at most one `decision.updated` (final) before `turn.completed`. |
| gap 13 / 14 (conditioning) | ADR-004 assumes `direct` renders no block and folds the template id into the signature. |
| gap 20 (cost lines) | IMPL-DEC-16. |
| gap 22 (outbox) | The decision row is written synchronously (IMPL-DEC-19). |
| gap 29 (escaping, spotlight) | `clean_text` and `escape_reserved_tags` (spec §5.2) should be the shared core helpers. |
| gaps 30 / 32 / 33 / 36 / 37 (contracts) | `not_found` for GET; facade 403/422/503; the out-of-phase error shape; `ProfileInfo.version` integer. |
| gap 39 | Bands come from the snapshot; acceptance thresholds come from `labeling.acceptance.*` (ADR-003). |
| gap 41 | `check_examples` is importable, so the test-mode validator runs `result_invariants`, `jev_invariants` and `oa_invariants`. |
| B1 interface additions | spec §1.2. |
| foundation `config/rules.py` | the P1 rules P1-D1…D6. |
| ops-admission | the worst-case call formula (spec §6.5). |
