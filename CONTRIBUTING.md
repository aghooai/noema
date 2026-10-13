# Contributing

Noema is built by roles (people or agents), each owning a set of paths. This file is the workflow: who may change
what, how frozen areas change, and what must pass before a change is handed off. Setup and tests:
[docs/development/](docs/development/README.md).

## 1. Non-negotiable rules

1. **Bedrock only.** Every model call goes to Amazon Bedrock through `noema.adapters`. Never call the OpenAI API or
   any other model API, never import `openai`, `anthropic`, `requests` or `boto3` in `src/` (`make forbidden-scan`,
   import-linter). The `openai` package is used only by a facade conformance test pointed at a local server.
2. **No live calls outside the live smoke.** Tests run on the wire mock and moto; the live smoke has a hard stop of
   87 ledger entries in total ([docs/development/testing.md](docs/development/testing.md) section 6).
3. **No new dependencies.** `pyproject.toml`, `uv.lock`, `package.json` and `package-lock.json` change only through
   the platform role (`make lock`). Use `uv run --frozen --no-sync` (the Makefile does).
4. **Only your paths.** Edit only paths your role owns (section 2); anything else is a change request (section 4).
5. **No git write commands by roles.** The orchestrator commits and tags between waves after running the gates.
6. **Every claim measured.** A report or doc states a number only with the run that produced it.

## 2. Ownership (`OWNERS.yaml`)

`OWNERS.yaml` maps path globs to roles. Rules are ordered: the **first** rule whose pattern matches a path decides
its owners (`*` stays in one segment, `**` spans segments, `{a,b}` are alternatives, `<role>` matches a role name).
`tools/check_ownership.py` enforces it:

```bash
make ownership                  # every changed path (git status, read-only) has an owner; exit 1 otherwise
make ownership ROLE=<role>      # fails on any changed path that <role> does not own
make qa-ownership-all           # every tracked path has an owner
uv run --frozen --no-sync python tools/check_ownership.py --owner-of <path>
uv run --frozen --no-sync python tools/check_ownership.py --paths <path> [<path> ...]
```

`make ownership` is a `make check` step, so a new file outside every rule fails the gate until `OWNERS.yaml` (a
platform file) gets a rule. Each module's role also owns its tests (`tests/{unit,integration}/<module>/`) and its
handoff doc `docs/modules/<module>.md`.

## 3. Frozen areas

Roles marked `frozen: true` in `OWNERS.yaml` were frozen at the Wave B interface-freeze tag (`foundation-freeze`):
after it only the orchestrator edits their paths.

| Frozen role | Paths (summary) |
|---|---|
| platform | `src/noema/{core,contracts,config}/**`, the root tooling (`pyproject.toml`, `uv.lock`, `Makefile`, `.importlinter`, `OWNERS.yaml`, `conftest.py`, `.env.example`), `make/{common,lint}.mk`, `tools/{check_ownership,sync_contracts}.py` |
| db-schema, db-repos | `src/noema/db/**` (Alembic revisions, roles, RLS, definer functions, `repos/`: the only SQL), `tests/integration/db*/**`, `make/db.mk` |
| api | `src/noema/api/**` (app, middleware, SSE framing; route bodies belong to the route's domain owner), `make/api.mk` |
| fakes-telemetry | `src/noema/telemetry/**`, `tests/{fakes,contract_internal,golden}/**` |
| wave-a | `docs/IMPL-DECISIONS.md`, `docs/impl-spec/**`, `docs/adr/**`, `docs/impl-decisions/**` |

The contracts are frozen twice: the canonical copy is the design repo's `contracts/`, vendored byte for byte
(`make contracts-check` fails on drift). A contract change is made in the design repo first (with its
`check_docs.sh` green), then `make contracts-sync contracts-generate contracts-check` by the platform role.

## 4. Change requests

When a change needs a path you do not own (a frozen Protocol, a schema change, an `OWNERS.yaml` rule, a
dependency), write it to `.orchestration/requests/<your-role>.md` (the one file every role owns for this):
- one numbered item per request, with a bold one-line title;
- what is wrong or missing, with file and line or spec section, and the exact change you propose;
- what depends on it, and whether it blocks you.

The orchestrator applies requests between waves (Wave C2a applied the C1 requests to the frozen areas, for example
`db` revision `0002_c2a_requests`), records rulings as `IMPL-0nn` amendments in `docs/IMPL-DECISIONS.md`, and
marks each item done in the request file. Decisions that change externally visible behavior are material and go
to the owner first (`docs/impl-spec/OWNER-CHECKPOINT.md`).

## 5. Before a handoff

| Check | Command |
|---|---|
| your paths only | `make ownership ROLE=<role>` |
| lint, types, layers, tests | `make lint MOD=<m>`, `make typecheck MOD=<m>`, `make test MOD=<m>`, then `make check` (needs `make up`) |
| stack behavior, if you touched it | `make e2e` on a stack of your own (`E2E_PROJECT`, `E2E_PORT_BASE`) |
| the gate you affect | its QA target ([docs/development/testing.md](docs/development/testing.md) section 3) |
| docs | update `docs/modules/<module>.md` (wiring, decisions, known limits) and any operator doc your change affects |

Style: ruff (line length 120) and mypy `--strict` on sources and tests; tests are deterministic and isolated
([docs/development/testing.md](docs/development/testing.md) section 4); a failing gate is reported as FAIL with its
evidence, never softened.

## 6. Commits

Only the orchestrator commits, one commit per wave, tagged (`wave-a-design`, `foundation-freeze`, `wave-c1`,
`wave-c2a`, `wave-c2b`, `wave-e`, `wave-e2`, `wave-e3`), after `make check`, `make observability-check` (from Wave
F on: the promtool tests and the runbook-anchor check, which `make check` does not run) and the wave's gates pass.
The commit message summarizes the wave, its measured results and its open items.
