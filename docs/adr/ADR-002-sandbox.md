# ADR-002: Media sandbox (nsjail in a network-less container, bwrap for host dev, inproc-rlimit for unit tests)

- Status: adopted by IMPL-081 (Wave A, 2026-10-09; reviewed in Wave A2). Errata applied in place in Wave C2a (2026-10-10) from the B2 infra and C1 media-pipeline evidence: §2 privileges and outer seccomp, §3 template fields, slot rules, §4 syscall 166, §5 output budget and queue bound, §6 `started` line, cancel on close and spool lifetime, §8 recipe.
- Owners: security architect (author), media-pipeline (implements `media/sandbox/`, `media/extractors/`, `media/worker`), foundation/B1 (images, compose, selftest).
- Resolves: **IMPL-081** (sandbox) and the sandbox half of **IMPL-029f/g** in [`../IMPL-DECISIONS.md`](../IMPL-DECISIONS.md) (canonical; the analyst's `security.md`, with its ids IMPL-SEC-N7/N8/29f/29g, was never written). "09-security §n" below refers to [`../impl-spec/09-security.md`](../impl-spec/09-security.md).
- Refs: 02-architecture.md:36-37, :56-57, :134, :732-754, :760-763, :817-824, :881; 04-caching-routing-security.md:193, :227-241; 06-validation-plan.md:222; 03-contracts.md:406-422; 08-technical-reference.md:484; 01-feasibility-study.md:427; PLAN.md "nsjail privileges"; 02-interfaces-ownership.md risk 5 (`SandboxRunner`).

## Context

Z3 has no network and no credentials (02:36). Untrusted bytes go in; only re-encoded derivatives come out (02:134, 04:240).

The parsers in scope for Phase 1 have real CVE histories:
- pikepdf/qpdf, Docling (docling-parse + pypdfium2/PDFium + torch);
- Pillow + pillow-heif (libheif);
- Tesseract/Leptonica, libmagic, Magika (onnxruntime), markdown-it-py (nh3 is not used in Phase 1, 09-security §6.5).

nsjail inside Docker needs `CAP_SYS_ADMIN` (namespaces, mounts, pivot_root), and on some hosts a relaxed outer seccomp/AppArmor profile. That weakens the container boundary. The plan therefore removes the container's network entirely (`network_mode: none`).

A container with no network can't reach Postgres (job queue) or S3 (quarantine, derivatives), so the job consumer and the jailer must be split.

Host facts (checked 2026-10-09, 03-env): Docker 25.0.8, cgroup v2, builtin seccomp, no AppArmor/SELinux listed. `bwrap 0.10.0` works unprivileged (`--unshare-all` leaves only `lo`). The host has **no nsjail, tesseract, qpdf, pdftoppm or magika CLI**, and its libmagic is `file` 5.39. The plan therefore depends on a **Debian media image** (§8) that carries all of them. Every sandbox and extractor test that needs them runs in that image under compose (spec-07 §7).

## Decision

### 1. Topology: a networked spooler and a network-less jailer

```text
 Z2  media-worker  (api image; NO media libraries; compose network: postgres, minio)
       │ 1. JobQueue (SKIP LOCKED) → 2. GET quarantine object → /spool/<job>/in/input.bin
       │ 3. connect /spool/sandboxd.sock (UDS) → request JSON ─────────────┐
       │ 6. validate manifest + outputs (trusted, stdlib only) → PUT derivatives, update DB
 ───── shared volume `media-spool` (tmpfs, 4 GiB) — the ONLY channel ──────┼─────────────
 Z3  media-sandbox (media image; network_mode: none; no secrets; no env credentials)
       │ 4. sandboxd: SO_PEERCRED uid check → validate request → spawn nsjail (one jail per job)
       │ 5. jail: python -I -m noema.media.extractors.entry <kind> → /work/out/* + manifest.json
       └─────────────────────────────────────────────────────────────────────┘ response JSON
```

- The **media-worker** owns every network side effect and runs no parser for untrusted media. import-linter forbids `noema.media.worker`, `noema.api`, `noema.turns`, `noema.decision`, `noema.generation` and `noema.jobs` from importing `PIL`, `pillow_heif`, `pikepdf`, `docling*`, `pypdfium2`, `magika`, `magic` and `markdown_it` (the full list is spec-01 §2 rule 7). Only `noema.media.extractors` may.
- The **media-sandbox** holds no credentials:
  - no AWS, DB or Redis environment variables (startup asserts that no `AWS_*`, `NOEMA_DB_*` or `*_SECRET*` variables are set);
  - a read-only root filesystem;
  - a container escape lands in a namespace with only `lo`.
- Inline images (turns and `/v1/decisions`) take the same path, as a high-priority job; the request waits only for the `image.normalize` result (`raster`, `raster_1568`) ([`../impl-spec/07-media.md`](../impl-spec/07-media.md) §3.2). The API never decodes media (IMPL-074).

### 2. Container privileges

| Container | Network | Capabilities | Outer seccomp / AppArmor | User | Filesystem | Limits |
|---|---|---|---|---|---|---|
| `media-worker` | compose internal network | `cap_drop: [ALL]` | Docker default | 10001:10002 | `read_only: true`, tmpfs `/tmp`, `/spool` rw | `mem_limit: 1g`, `pids_limit: 256` |
| `media-sandbox` | `network_mode: none` | `cap_drop: [ALL]` + `[SYS_ADMIN, SETUID, SETGID, CHOWN, DAC_OVERRIDE, FOWNER, KILL]` (adopted; nsjail does not need `KILL`, kept until SEC-SBX-05 shows sandboxd doesn't) | `deploy/sandbox/docker-seccomp.json` = Docker's default profile plus `pivot_root` for `CAP_SYS_ADMIN` (Docker's default never allows `pivot_root`, Docker 25.0.8; narrower than the `seccomp=unconfined` fallback, which is not needed); `no-new-privileges:true`; `systempaths` default (jails use `mount_proc: false`) | `sandboxd` as container root; jails as uid `100000+slot` | `read_only: true`, tmpfs `/tmp`, `/spool` rw, models and venv read-only | `mem_limit: 6g`, `cpus: 2`, `pids_limit: 1024` |

*Errata (Wave C2a, evidence `deploy/sandbox/PRIVILEGES.md`, `tests/integration/infra/test_sandbox_privileges.py`): the row above is the adopted set.* The B1 platform engineer adds `deploy/sandbox/selftest.sh`, which runs in CI on the media image. It records the **minimal** working set of capabilities and outer seccomp/AppArmor/systempaths in `deploy/sandbox/PRIVILEGES.md`, starting from the target column and relaxing one item at a time.

The selftest must prove, inside a jail, that:
1. `socket(AF_INET)` fails;
2. no interface other than `lo` exists, and `lo` is down (`iface_no_lo: true`);
3. `/spool` outside the job dir is invisible;
4. `/proc/1/environ` of the container is unreadable;
5. `ptrace` and `io_uring_setup` kill the process with SIGSYS;
6. writing outside `/work/out` and `/tmp` fails;
7. a 2× `rlimit_as` allocation dies;
8. the time limit kills a busy loop;
9. `unshare(CLONE_NEWUSER)`, `clone(CLONE_NEWUSER)`, `fsopen` and `bpf` kill the process with SIGSYS; `clone3` returns `ENOSYS`; `socket(AF_INET6)` and `socket(AF_NETLINK)` return `EAFNOSUPPORT`;
10. an x32-ABI `ptrace` (syscall number | `0x40000000`) is killed or returns `ENOSYS`. If it does neither, the policy gains an explicit x32 kill rule before the build passes.

### 3. nsjail configuration (one config per job kind, generated by sandboxd from a template)

```text
mode: ONCE
hostname: "sbx"
cwd: "/work"
time_limit: <wall_s>                   # per kind, table §5
max_cpus: 1
keep_env: false
envar: "LANG=C.UTF-8"  envar: "HOME=/tmp"  envar: "OMP_NUM_THREADS=1"  envar: "OPENBLAS_NUM_THREADS=1"
envar: "HF_HUB_OFFLINE=1"  envar: "TRANSFORMERS_OFFLINE=1"  envar: "DOCLING_ARTIFACTS_PATH=/opt/docling-models"
envar: "TESSDATA_PREFIX=/usr/share/tesseract-ocr/5/tessdata"
clone_newnet: true  clone_newuser: true  clone_newns: true  clone_newpid: true
clone_newipc: true  clone_newuts: true  clone_newcgroup: true
iface_no_lo: true
uidmap { inside_id: "65534" outside_id: "<100000+slot>" count: 1 }
gidmap { inside_id: "65534" outside_id: "<100000+slot>" count: 1 }
rlimit_as_type: VALUE  rlimit_as: <mem_mib>          # HARD/INF for pdf.markdown (torch), bounded by the container cgroup
rlimit_cpu_type: VALUE rlimit_cpu: <cpu_s>
rlimit_fsize: <fsize_mib>  rlimit_nofile: 64  rlimit_nproc: <nproc>  rlimit_stack: 8  rlimit_core: 0
rlimit_nproc_type: VALUE  rlimit_stack_type: VALUE    # nsjail 3.6 defaults these two to SOFT, which ignores the values
mount_proc: false                                    # true only if the selftest shows an extractor needs it
mount { src: "/usr" dst: "/usr" is_bind: true rw: false }
mount { src: "/lib" dst: "/lib" is_bind: true rw: false }
mount { src: "/lib64" dst: "/lib64" is_bind: true rw: false mandatory: false }
mount { src: "/etc/ld.so.cache" dst: "/etc/ld.so.cache" is_bind: true rw: false }
mount { src: "/opt/noema-sandbox" dst: "/opt/noema-sandbox" is_bind: true rw: false }   # venv + noema wheel
mount { src: "/opt/docling-models" dst: "/opt/docling-models" is_bind: true rw: false mandatory: false }
mount { src: "/dev/null" dst: "/dev/null" is_bind: true rw: true }
mount { src: "/dev/urandom" dst: "/dev/urandom" is_bind: true rw: false }
mount { dst: "/tmp" fstype: "tmpfs" rw: true options: "size=256m" nosuid: true nodev: true }   # flags are mount fields
mount { src: "/spool/<job_id>/in" dst: "/work/in" is_bind: true rw: false }
mount { src: "/spool/<job_id>/out" dst: "/work/out" is_bind: true rw: true }
# seccomp: nsjail --seccomp_policy /etc/noema-sandbox/seccomp.kafel (the image copy of deploy/sandbox/seccomp.kafel)
exec_bin { path: "/opt/noema-sandbox/venv/bin/python" arg: "-I" arg: "-m" arg: "noema.media.extractors.entry" arg: "<kind>" }
```

- Each concurrent jail slot gets its own outside uid (`100000+slot`). That makes `RLIMIT_NPROC` per job, not per pool.
- `sandboxd` caps the concurrency: total slots = `cpus` (2). The long kinds (`pdf.sanitize`, `pdf.render`, `pdf.markdown`, `ocr`) hold at most `slots − 1`, which keeps a slot for `detect`, `image.normalize` and `markdown.sanitize` (inline images). `pdf.markdown` (Docling) runs one at a time and takes its lock before a slot, so with 2 slots `pdf.render` and `pdf.markdown` of one PDF run one after the other.
- `no_new_privs` stays on, and `keep_caps: false` (both nsjail defaults): the jailed process has no capabilities in its user namespace.
- `pdf.markdown` has no `RLIMIT_AS` (torch reserves large address space), so its bound is the container `mem_limit`. An OOM kill there is SIGKILL → `failed{extraction_failed}`. If sandboxd itself dies, compose restarts the container (`restart: unless-stopped`), and the in-flight UDS calls fail → `failed{extraction_failed}`. SEC-SBX-05 asserts that the other slot keeps working.

### 4. Inner seccomp policy (`deploy/sandbox/seccomp.kafel`)

The policy is default-allow with a deny-list.

Why not an allow-list: a robust one for CPython + onnxruntime + torch + Tesseract is brittle, and a breakage would show up as false `failed` derivatives. The deny-list removes the kernel attack surface most used in container escapes. Namespaces remove the network, the credentials and the writable filesystem.

```text
POLICY noema_sandbox {
  KILL_PROCESS {
    ptrace, process_vm_readv, process_vm_writev, kcmp,
    bpf, perf_event_open, userfaultfd,
    io_uring_setup, io_uring_enter, io_uring_register,
    keyctl, add_key, request_key,
    init_module, finit_module, delete_module, kexec_load, kexec_file_load,
    mount, SYSCALL[166], pivot_root, chroot, setns, unshare,                 // 166 = umount2 (the pinned kafel names it umount)
    fsopen, fsconfig, fsmount, fspick, move_mount, open_tree, mount_setattr,
    open_by_handle_at, name_to_handle_at, pidfd_getfd,
    reboot, swapon, swapoff, acct, quotactl, quotactl_fd, syslog, iopl, ioperm, lookup_dcookie, fanotify_init,
    settimeofday, clock_settime, clock_adjtime, adjtimex,
    clone(flags, stack, ptid, ctid, tls) { (flags & 0x10000000) != 0 }     // CLONE_NEWUSER
  },
  ERRNO(38) { clone3 },                                                    // ENOSYS -> glibc falls back to clone()
  ERRNO(97) { socket(domain, type, protocol) { domain != 1 } }            // EAFNOSUPPORT except AF_UNIX
}
USE noema_sandbox DEFAULT ALLOW
```

Comments in the policy file are `//`: kafel reads `#` as a preprocessor directive. If the pinned kafel lacks a syscall name (the new mount API), the policy uses `SYSCALL[<x86_64 number>]`. The selftest verifies the kafel syntax and the behaviour (§2 checks 5, 9 and 10).

A SIGSYS exit (nsjail returns 128+31):
- maps to `status: "violation"`;
- the derivative is `failed` with `extraction_failed`;
- emits security event `sandbox.violation{kind, job_id}` and metric `aghoo_sandbox_violations_total{kind}`.

It is not a `rejected` file, because a library probe can trigger SIGSYS on benign input.

### 5. What runs inside, per job kind (limits are initial design values, tuned by media-pipeline in C1 with evidence)

| Kind | Inside the jail | Input (`in/input.bin`) and `params` | Outputs (`/work/out`) and facts | wall_s | cpu_s | mem_mib (AS) | fsize_mib | nproc | max_outputs |
|---|---|---|---|---|---|---|---|---|---|
| `detect` | Magika (`PredictionMode.HIGH_CONFIDENCE`) + libmagic (`python-magic`, `mime=True`), both on the whole file; a strict UTF-8 decode and a NUL scan | original; `{}` | `manifest.json` only; facts `magika_label`, `magika_score`, `libmagic_mime`, `bytes`, `utf8_strict`, `has_nul` | 15 | 10 | 3072 | 1 | 32 | 0 |
| `image.normalize` | Pillow + pillow-heif, exactly 09-security §6.4 | original; `{"format": "PNG\|JPEG\|WEBP\|GIF\|HEIF"}` | `raster.png`, `raster_1568.png` or `raster_1568.jpg`, `thumbnail.png`; facts `width`, `height` (original, after orientation), `frames` | 30 | 20 | 2048 | 64 | 16 | 3 |
| `pdf.sanitize` | pikepdf rewrite and post-scan (09-security §6.3) | original; `{}` | `pdf_sanitized.pdf`; facts `pages`, `encrypted`, `active_content[]` | 60 | 45 | 2048 | 100 | 16 | 1 |
| `pdf.render` | pypdfium2 (built without V8/XFA) renders every page at a 1568 px long edge | `pdf_sanitized`; `{"long_edge_px": 1568}` | `page-NNNN.png` (one per page, `page` set); facts `pages`, `text_layer_pages[]` (pages whose pdfium text page has ≥ 1 non-space char) | 300 | 280 | 2048 | 64 | 16 | 600 |
| `pdf.markdown` | Docling (`do_ocr=False`, models from `/opt/docling-models`, offline); per page `export_to_markdown(page_no=p, image_placeholder="")` → the 09-security §6.5 renderer → joined with `\f` (API names verified against the pinned docling-core in C1) | `pdf_sanitized`; `{}` | `markdown.md` (exactly `pages` segments); facts `pages` | 900 | 900 | unbounded (container `mem_limit`) | 64 | 64 | 1 |
| `ocr` | per page: pypdfium2 renders at 300 dpi (PDF), then `tesseract - stdout -l eng --psm 3` as a child process | `raster` (image; `{"pages": null}`) or `pdf_sanitized` (`{"pages": [n, …]}`, ascending) | `ocr_text.txt`: image → one segment; PDF → one segment per document page, `\f`-separated, empty for pages not listed; no facts | 300 | 280 | 1024 | 16 | 16 | 1 |
| `markdown.sanitize` | markdown-it-py `js-default` + the 09-security §6.5 renderer (no nh3) | original `.md`; `{}` | `markdown.md` (one segment) | 90 | 60 | 2048 | 16 | 8 | 1 |

Erratum (C2a, media-pipeline request 15): the `markdown.sanitize` row was 15 s / 10 s / 512 MiB, which failed a legitimate 1 MiB pipe table; with one tree at a time and the cyclic GC paused per pass, a 5 MiB input measured 30.4 s wall in 1,536 MiB AS (evidence: docs/modules/media_pipeline.md, Known limits).

`facts` and `params` are closed per kind (`additionalProperties: false`); sandboxd answers `bad_request` to unknown params. A scanned 600-page PDF can exceed the initial `ocr` limits; `ocr_text` then fails with `timeout` and the file is `partial`. C1 may split `ocr` into page chunks through `params.pages`, with no interface change.

`sandboxd` clamps every requested limit to the table maximum. The worker can't raise them. Each job also has an aggregate output budget (`noema.media.sandbox.limits.OUTPUT_BUDGET_BYTES`: `pdf.render` 512 MiB, the others about `max_outputs × fsize`): the extractor stops at it and the job fails, and the validator refuses a larger manifest (`manifest_invalid`). Timeouts are enforced twice:
- nsjail `time_limit`, which kills the jail;
- the worker's `wall_s + 5` bound on the UDS call, counted from sandboxd's `started` line (§6) → `status: timeout` → `failure.code: timeout`. The wait for a slot has its own bound (`QUEUE_WAIT_S` = 1,800 s → `failed{busy}` → `extraction_failed`).

### 6. IPC contract (UDS `/spool/sandboxd.sock`, one request per connection, newline-delimited JSON, ≤ 64 KiB each way)

```jsonc
// request (media-worker -> sandboxd)
{"v": 1, "job_id": "sbx_<ULID>", "kind": "pdf.render",
 "input": "in/input.bin",            // fixed name; the dir is /spool/<job_id>/
 "params": {"long_edge_px": 1568},     // closed per kind (§5)
 "limits": {"wall_s": 300, "cpu_s": 280, "mem_mib": 2048, "fsize_mib": 64, "nproc": 16, "max_outputs": 600}}
// sent when the jail starts, before the response (the worker's wall_s + 5 bound starts here)
{"v": 1, "job_id": "sbx_<ULID>", "event": "started"}
// response (sandboxd -> media-worker)
{"v": 1, "job_id": "sbx_<ULID>", "status": "ok|failed|timeout|violation|bad_request",
 "exit_code": 0, "signal": null, "wall_ms": 1234, "manifest": "out/manifest.json",
 "stderr_tail": "<= 2 KiB, redacted, never logged above DEBUG>"}
```

```jsonc
// out/manifest.json (written INSIDE the jail -> untrusted; ≤ 1 MiB)
{"v": 1, "kind": "pdf.render", "extractor": "pypdfium2@<pinned>",
 "outputs": [{"path": "out/page-0001.png", "kind": "page_images", "media_type": "image/png",
              "bytes": 182733, "sha256": "<64 hex>", "page": 1, "width": 1568, "height": 2030}],
 "facts": {"pages": 12, "text_layer_pages": [1, 2]},
 "rejection": null,                       // or {"code": "<FileObject.rejection.code enum>"}
 "warnings": ["<enum values only>"]}
```

`sandboxd` checks before spawning:
- `SO_PEERCRED` uid == 10001, right after `accept` (it reads nothing from a refused peer); the request line must arrive within 5 s (`READ_TIMEOUT_S`); both refusals answer `bad_request` with `job_id` `""`, which the worker accepts (C2a erratum);
- `job_id` matches `^sbx_[0-9A-HJKMNP-TV-Z]{26}$`;
- `kind` is in the enum;
- `/spool/<job_id>/{in,out}` are real directories, opened `O_DIRECTORY|O_NOFOLLOW` (no symlinks);
- `in/input.bin` is a regular file.

Spool layout and ownership (media-worker uid 10001 creates it): `/spool/<job_id>/` 0711, `in/` 0755, `in/input.bin` 0644, `out/` 0700. Before the run, sandboxd chowns `out/` (the directory only) to the slot uid. After the run, it walks `out/` with `os.scandir` and `follow_symlinks=False`, and deletes every entry that is not a regular file with `st_nlink == 1` (symlinks, directories, FIFOs, devices, hard links). It then opens each remaining file with `O_NOFOLLOW` and calls `fchown(fd, 10001, 10002)` and `fchmod(fd, 0o640)`; finally `out/` itself (opened `O_DIRECTORY|O_NOFOLLOW`) goes back to `10001:10002`, mode 0750. It never calls a path-based `chown`/`chmod` on jail-written names, because a root helper following a jail-planted symlink would change files in the container.

The **media-worker** then validates the result with trusted code (`noema.media.sandbox.manifest.validate(...) -> ExtractResult`, stdlib only):
1. The manifest is ≤ 1 MiB and is strict JSON matching the schema (`additionalProperties: false`, enums for `kind`/`media_type`/`warnings`/`rejection.code`).
2. Each `path` matches `^out/[a-z0-9_-]{1,64}\.(png|jpg|pdf|md|txt)$`, is opened with `O_NOFOLLOW`, and is a regular file. Its size equals `bytes` and is ≤ the per-kind cap. Its `sha256` matches. Its magic bytes match `media_type` (`\x89PNG\r\n\x1a\n`, `%PDF-`, JPEG `FF D8 FF`). Text is strict UTF-8 with no NUL, and `.md` text passes the reserved-tag pre-scan only for metrics (escaping happens at prompt render, 09-security §5).
3. The count is ≤ `max_outputs`. Files not listed in the manifest are deleted, never uploaded.
4. On any violation: status `failed` (`extraction_failed`) and a `sandbox.manifest_invalid` security event.

Result mapping (worker; spec-07 §1.2):

| sandboxd `status` | Manifest | `ExtractResult.status` | Derivative / file |
|---|---|---|---|
| `ok` | valid, `rejection: null` | `ok` | outputs stored, derivatives `ready` |
| `ok` | valid, `rejection: {code}` | `rejected` | file `rejected{code}` |
| `ok` | invalid | `failed` | `failed{extraction_failed}` + `sandbox.manifest_invalid` |
| `failed` (non-zero exit, SIGKILL/OOM) | ignored | `failed` | `failed{extraction_failed}` (`ocr`: `ocr_failed`) |
| `timeout` (nsjail `time_limit` or the worker's `fail_after`) | ignored | `timeout` | `failed{timeout}` |
| `violation` (SIGSYS, exit 159) | ignored | `violation` | `failed{extraction_failed}` + `sandbox.violation` |
| `bad_request` | — | `failed` | `failed{extraction_failed}`; an ERROR log (a worker bug) |

Outputs are untrusted even after validation:
- Bedrock and the browser decode them, never Z2;
- the UI serves them only as authenticated `attachment` downloads or blob images (09-security §6.8, §8).

Closing the connection cancels the job: a queued job never runs, and a running jail is killed.

A failed connect to sandboxd dispatches nothing: `RemoteSandboxRunner.run` raises `SandboxUnavailable` and `media.process` retries the job (never a file failure). A failure after the connect stays `failed{unavailable}` (C2a erratum, §3/§6).

Each job directory is deleted as soon as its outputs are stored, on success or failure. The inputs that later jobs of the same file read (the original, `pdf_sanitized`, `raster`) stay in one keep directory per run (`sbx_<ULID>`, so the orphan sweep covers a crashed run); they are hard-linked into the job directories and released after their last reader. sandboxd sweeps orphans older than 1 h.

### 7. Backends (`SandboxRunner` Protocol, frozen in B1)

```python
# noema/core/protocols/sandbox.py
SandboxKind = Literal["detect", "image.normalize", "pdf.sanitize", "pdf.render", "pdf.markdown", "ocr", "markdown.sanitize"]

@dataclass(frozen=True, slots=True)
class SandboxLimits:
    wall_s: float; cpu_s: int; mem_mib: int | None; fsize_mib: int; nproc: int; max_outputs: int

@dataclass(frozen=True, slots=True)
class SandboxJob:
    job_id: str; kind: SandboxKind; input_path: Path; params: Mapping[str, JsonValue]

@dataclass(frozen=True, slots=True)
class SandboxOutput:
    path: Path; kind: str; media_type: str; bytes: int; sha256: str
    page: int | None = None; width: int | None = None; height: int | None = None

@dataclass(frozen=True, slots=True)
class ExtractResult:
    status: Literal["ok", "rejected", "failed", "timeout", "violation"]
    outputs: tuple[SandboxOutput, ...]; facts: Mapping[str, JsonValue]
    rejection_code: str | None; wall_ms: int; extractor: str

class SandboxRunner(Protocol):
    async def run(self, job: SandboxJob, limits: SandboxLimits) -> ExtractResult: ...
```

| Backend | Where | Isolation | Allowed `NOEMA_ENV` |
|---|---|---|---|
| `RemoteSandboxRunner` | media-worker (compose, staging, prod) | UDS client to `sandboxd` → `NsjailRunner` | test, dev, staging, prod (**required** in staging/prod) |
| `NsjailRunner` | inside `sandboxd` only | §3–§4 | test, dev, staging, prod |
| `BwrapRunner` | host dev without Docker | `bwrap --unshare-all --die-with-parent --new-session --clearenv --cap-drop ALL --ro-bind /usr /usr --symlink usr/lib /lib --symlink usr/lib64 /lib64 --ro-bind /etc/ld.so.cache /etc/ld.so.cache --ro-bind <venv> <venv> --ro-bind <python_home> <python_home> --dev /dev --tmpfs /tmp --ro-bind <in> /work/in --bind <out> /work/out --chdir /work` plus the §3 `envar` values via `--setenv` (`<python_home>` = `sys.base_prefix`; uv-managed interpreters live outside `/usr`), rlimits set in `preexec_fn`, and the anyio timeout. **No seccomp**: namespace and rlimit isolation only, so it isn't a security boundary claim. On this host (merged `/usr`, no tesseract) `ocr` is unavailable: `BwrapRunner` probes `tesseract` on `PATH` at start, and an unavailable kind returns `failed` with `extractor: "unavailable"`, so the dependent tests skip | test, dev |
| `InprocRlimitRunner` | unit tests | Child `python -I` process with `RLIMIT_AS/CPU/FSIZE/NOFILE/CORE` only; no namespaces, so no network isolation | test only |

`assert_backend_allowed(backend, env)` runs at process start. A disallowed combination raises `ForbiddenInEnvironment` and the process exits non-zero. Unit test SEC-SBX-07 covers every cell of this table.

### 8. Images (foundation/B1 `deploy/docker/`; media-pipeline owns the `media` dependency group)

| Image | Base | Contents | Used by |
|---|---|---|---|
| `noema-api` | `python:3.12-slim-bookworm` (Debian 12) | `uv sync --frozen --no-dev` **without** the `media` dependency group, so it holds no Pillow, pillow-heif, pikepdf, Docling, torch, pypdfium2, magika, python-magic, markdown-it-py or libmagic | `api`, `jobs`, `media-worker` |
| `noema-media` | `python:3.12-slim-bookworm` (Debian 12) + a builder stage | **nsjail** built from upstream tag 3.6 (commit `f78475530b46`), with its bundled kafel (build deps: `protobuf-compiler`, `libprotobuf-dev`, `libnl-route-3-dev`, `bison`, `flex`); runtime `libprotobuf32`, `libnl-route-3-200`; **tesseract-ocr 5 + tesseract-ocr-eng** (`/usr/share/tesseract-ocr/5/tessdata`); **libmagic1 + libmagic-mgc**; **qpdf** (CLI for `qpdf --check` in tests and the selftest only; pikepdf uses its bundled libqpdf); the venv `/opt/noema-sandbox/venv` = `uv sync --frozen --no-dev --group media` (pikepdf, Pillow, pillow-heif, pypdfium2, docling + docling-parse, torch **CPU-only wheel**, magika + onnxruntime, python-magic, markdown-it-py) plus the `noema` wheel; the Docling models baked into `/opt/docling-models` at build time (`docling-tools models download`); `deploy/sandbox/{seccomp.kafel,selftest.sh}` installed under `/etc/noema-sandbox/` | `media-sandbox` |

- Not in any image: ImageMagick, Ghostscript, LibreOffice, ffmpeg, poppler (Phase 2 media or unused).
- Both images pin `python:3.12-slim-bookworm` by digest. `trivy image` scans both (09-security §9). The media image runs no network at run time: every model and tessdata is baked in.
- Debian 12 is merged-`/usr`: `/lib` and `/lib64` are symlinks. The nsjail `mount` entries for them bind the resolved directories; selftest check 1 runs only if the dynamic loader works, which proves this.
- The Python runtime is `/usr/local/bin/python3.12` (covered by the `/usr` mount); the venv's `python` symlinks to it.

## Tests (gate 06:222 "no crash escapes, no egress, only re-encoded derivatives leave")

| Id | What | Pass |
|---|---|---|
| SEC-SBX-01 | `selftest.sh` checks 1–10 (§2) in CI on the media image | all pass; `PRIVILEGES.md` updated |
| SEC-SBX-02 | Egress: an extractor test hook (test builds only, `NOEMA_ENV=test`) tries TCP to `10.0.0.1:80`, UDP DNS and `AF_NETLINK` | `EAFNOSUPPORT`/`ENETUNREACH`; no packet on the container side; `network_mode: none` asserted from `docker inspect` |
| SEC-SBX-03 | Fuzz corpus (06:222): malformed PDFs, JS/OpenAction/EmbeddedFile/XFA/RichMedia PDFs, pixel bombs (PNG 50k×50k), GIF/WebP frame bombs, truncated HEIC, mitra polyglots (PDF+ZIP, PNG+PDF), zip bomb renamed `.pdf`, a 2 GB sparse file | no worker or API crash; each ends `rejected` (typed) or `failed`; no output leaves without manifest validation |
| SEC-SBX-04 | Manifest attacks (a compromised-extractor stub): symlink output, `../` path, size lie, sha256 lie, PNG magic on HTML, 10,001 outputs, 2 MiB manifest; plus `out/x.png` → symlink to `/etc/passwd`, a hard link, a FIFO and a subdirectory planted in `out/` | all rejected by the worker validator, with `sandbox.manifest_invalid` events; the planted symlink's target keeps its owner and mode (sandboxd never follows it), and the planted entries are deleted |
| SEC-SBX-05 | Limits: busy loop, 2× memory, fork bomb, 1 GiB write | killed; `timeout`/`failed`; the other jail slot keeps working |
| SEC-SBX-06 | No credentials: inside a jail and in `media-sandbox` env, no `AWS_*`, DB or Redis variables; no `/root/.aws` | asserted |
| SEC-SBX-07 | Backend/env matrix (§7) | every disallowed cell refuses to start |
| SEC-SBX-08 | An AFL/libFuzzer-style job (atheris) on `noema.media.extractors.entry` per kind, run nightly, not in `make check` | crashes contained; findings filed |

## Consequences

- Each sandbox job costs one extra process spawn and a spool copy, ≈10–50 ms. That's acceptable for uploads. For inline images it adds to the turn's preflight, which media-pipeline measures in C2.
- `media-sandbox` still runs with `CAP_SYS_ADMIN`, and perhaps a relaxed outer seccomp. The residual risk is a kernel exploit through an allowed syscall, escaping the user namespace and then the container. Mitigations: the inner kafel deny-list, a network-less container with no secrets, re-encoded outputs only, a patched-kernel policy for hosts, and the minimal privilege set recorded by the selftest. Accepted for Phase 1, with the record kept in `PRIVILEGES.md`.
- The networked worker has no parser libraries, so a parser exploit can't reach the network or credentials without a second, kernel-level escape.

## Alternatives rejected

- **One networked container with nsjail `clone_newnet`**: a jail escape lands next to DB/S3 credentials and the network, with `SYS_ADMIN`.
- **gVisor (`runsc`) / Firecracker**: stronger isolation, but they need host runtime installation (no IAM/AWS changes, and the compose-only environment). Recommended for Phase 2 production on EKS (gVisor RuntimeClass).
- **Dangerzone-style render-to-pixels for every PDF**: loses the text layer that Docling and Claude use. `page_images` gives the pixel path where it's needed.
- **A seccomp allow-list**: brittle with torch/onnxruntime. Revisit with `strace`-derived lists in Phase 2.
