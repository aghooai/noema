# Security

Noema handles tenant prompts, documents and images, signs Bedrock calls with each tenant's own AWS credentials,
and stores conversation history. This file summarizes the threat model, how to report a problem, and the
supply-chain gates. The full specification is spec-09 ([docs/impl-spec/09-security.md](docs/impl-spec/09-security.md)),
ADR-001 (tenancy and credentials) and ADR-002 (media sandbox); the measured results are SE1-SE15 in
[reports/qa/wave-e-summary.md](reports/qa/wave-e-summary.md) and [reports/qa/security.md](reports/qa/security.md).

## 1. Reporting a vulnerability

The repository has no remote, public issue tracker or security mailbox in Phase 1. Report privately to the
project owner, never in a shared document or chat channel. Include:
- the affected component (path or endpoint), version (`git describe --tags`) and environment;
- the steps to reproduce on a local stack (`make e2e-up`), with the request ids of the responses involved;
- the impact you observed (cross-tenant data, credential exposure, sandbox escape, injection outcome).

Never put a real API key, operator key, AWS credential or tenant content in a report; the dev stack's `ak_test_`
keys and fake AWS keys are fine. A confirmed finding is fixed by the owning role (`OWNERS.yaml`) with a regression
test in the matching suite (`tests/security`, `tests/fault`, a module's tests), and the gate it touches is re-run.

## 2. Threat model summary

Assets: tenant content (prompts, history, uploads, derivatives, decisions), API keys and operator keys, tenant AWS
sessions, the platform secrets (peppers, the database DSN, ExternalIds), the platform config, billing records.

| Boundary | Threats | Phase 1 controls | Evidence |
|---|---|---|---|
| Clients to the api | oversized bodies, DoS, credential stuffing | 50 MiB body cap (before parsing, after authentication for undeclared lengths), upload caps, at most 64 media parts; 256-bit keys stored as HMAC-SHA256 with a versioned pepper; one uniform 401; per-IP bucket on 401s; tenant-fair per-process cap | SE12, SE15 |
| Inside the api | IDOR, cross-project or cross-tenant reads | the principal comes only from the key row; a role and scope matrix (IMPL-083); RLS with FORCE on every tenant table, `noema_app` without BYPASSRLS; principal-bound idempotency | SE1: 356 cells, 0 leaks; SE2 |
| Caches and stores | cross-tenant cache hits, key misuse | per-tenant HMAC input digests and tenant-prefixed L2 keys; per-tenant SSE-KMS keys for objects; pub/sub payloads checked against the local tenant | SE3 (L2 half; the Bedrock half is NOT-RUN [P0]) |
| api to Bedrock (tenant accounts) | cross-tenant invocation, confused deputy, credential leak | one STS session per (pod, tenant) with an ExternalId, held in memory only; the role ARN allowlisted; the account asserted on every call; fail closed (503, never another credential) | SE4 on moto (AWS and CloudTrail: NOT-RUN [P0]) |
| Untrusted media to the sandbox | parser RCE, bombs, polyglots, egress | `network_mode: none` container, nsjail per job (namespaces, seccomp, rlimits), only listed re-encoded outputs stored; sanitized PDF, Markdown sanitization | SE9: fuzz 6/6, contained |
| Inputs to prompts | prompt injection, `<decision_context>` forgery, filename leaks | reserved-tag escaping, datamarking of document and OCR text (IMPL-086), filenames never sent to models, remote image URLs refused | SE6, SE7 (policy), SE8 |
| Model output to clients | malicious Markdown, active content | outputs are data, distributions range-checked, `reasoningContent` never forwarded; the UI renders sanitized Markdown under a strict CSP | SE11 |
| Operators to the config | a malicious or wrong policy push | two-person `noema-admin config propose` / `approve`, validated at propose and at load, audit rows; the database verifies each operator key (migration 0003) | SE15 |
| Everything to telemetry | secrets or content in logs, spans, errors | the redaction filter; content-free logs; 401s never echo the key | SE12 |

Known gaps and accepted risks (Phase 1):
- No edge (TLS termination, WAF) or OIDC/BFF login: compose exposes the api directly; the UI's cookie proxy is
  dev-only (`NOEMA_ENV=dev`). A production deployment must add the edge.
- Not run by design: the pre-GA penetration test (SE14), labeler poisoning and decision prompt-injection flip rates
  (SE5, SE10: need the Phase 0 `adversarial-v1` set), `injection_suspected` recall (SE7), the Bedrock-side cache
  isolation (SE3) and cross-account tests against real AWS accounts (SE4).
- `injection_suspected` is non-blocking (owner answer 2026-10-10); documents and OCR text are always datamarked
  instead (IMPL-086).
- A `reviewer` sees redacted excerpts of other projects' dropped labeling items in the same tenant (IMPL-042).
- media-sandbox needs `CAP_SYS_ADMIN` and a seccomp profile that allows `pivot_root` for nsjail
  ([deploy/sandbox/PRIVILEGES.md](deploy/sandbox/PRIVILEGES.md)); its blast radius is a network-less container.
- Keep Postgres statement logging with parameters off: the operator proof is a bind parameter (0003).

## 3. Supply chain

| Control | Where | Final tree ([reports/qa/wave-e-summary.md](reports/qa/wave-e-summary.md) SE13) |
|---|---|---|
| locked dependencies | `uv.lock` (`uv sync --frozen`), `package-lock.json` (`npm ci --offline --ignore-scripts`); only the platform role changes them | - |
| Python audit | `pip-audit` over `uv.lock` (in `make qa-security-sca`) | 0 findings in 243 packages |
| npm audit | both lockfiles | 0 / 0 |
| image scan and SBOM | `make image-scan`: trivy per image, CycloneDX SBOM per image, fails on a CRITICAL not waived | exit 0; SBOMs 155 / 155 / 331 components; api/worker 44 HIGH, media 4 CRITICAL + 94 HIGH, every one waived (the Wave F api image: 0 CRITICAL, 43 HIGH, all waived, [reports/perf/sizing.md](reports/perf/sizing.md) §8) |
| secret scan | gitleaks on the repository and on every security-stack capture | every hit triaged as a test vector, a moto key or an id |
| image contents | base images pinned by digest; no dev dependencies, tests or media parsers in the api and worker images; non-root uid 10001, read-only root, `cap_drop: ALL` | - |
| forbidden clients | `make forbidden-scan`, import-linter | clean |

### Waivers

`deploy/docker/.trivyignore.yaml` (owner: infra) lists every waived CVE: one entry per id with a `statement` (why
the vulnerable code is not reachable in Noema) and a review date `expired_at`. 51 ids were waived on the final
Wave E3 tree, all Debian OS packages without a fixed version, reviewed by 2027-01-09. An expired entry no longer
waives, so `make image-scan` and the SE13 gate (`tests.security.sca_gate`) fail until it is reviewed. A pip-audit id
can be waived in the same file; an npm high or critical finding cannot. Drop an entry once Debian ships a fix (the
image build applies `apt-get upgrade`) or the package leaves the image. A base image or interpreter change means a
re-scan and a review of the file. The api and worker images run the python-build-standalone CPython, which links
its own OpenSSL, SQLite, zlib, expat, libffi, xz, bzip2, ncurses and libedit statically: trivy's Debian package scan
cannot see them, so they are reviewed with each `PYTHON_VERSION` or `UV_IMAGE` bump in `deploy/docker/api.Dockerfile`.
