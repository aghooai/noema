# Media-sandbox privileges (ADR-002 §2, SEC-SBX-01)

The minimal working set of container privileges for `media-sandbox`, found by running `selftest.py` (ADR-002 §2
checks 1-10) from the ADR's target column and changing one item at a time. Each row of the ladder is an executable
test: `tests/integration/infra/test_sandbox_privileges.py` (host, `make test-integration MOD=infra`) and
`tests/integration/infra/media_image/` (in the image, `make test-media`). Re-run with `make sandbox-selftest`.

Recorded 2026-10-10 on: Docker 25.0.8, cgroup v2, builtin seccomp, no AppArmor/SELinux; kernel 6.1.128
(Amazon Linux 2023) with `CONFIG_X86_X32_ABI` not set; image `noema-media:dev` (nsjail 3.6, commit `f7847553`).

## Adopted settings (deploy/compose/docker-compose.yml, service `media-sandbox`)

| Item | Value | Why |
|---|---|---|
| network | `network_mode: none` | ADR-002 §1: a container escape lands next to `lo` only |
| capabilities | `cap_drop: [ALL]`, `cap_add: [SYS_ADMIN, SETUID, SETGID, CHOWN, DAC_OVERRIDE, FOWNER, KILL]` | the ADR target set, unchanged (below) |
| outer seccomp | `seccomp=../sandbox/docker-seccomp.json`: Docker's default profile plus one rule allowing `pivot_root` for `CAP_SYS_ADMIN` | Docker's default profile never allows `pivot_root`, which nsjail needs for its mount namespace |
| no-new-privileges | `no-new-privileges:true` | not in the ADR table; nsjail works with it (selftest 16/16), so it is kept as a free reduction |
| AppArmor / SELinux | none on this host | — |
| systempaths | Docker default (masked `/proc` paths) | enough while jails use `mount_proc: false` |
| root filesystem | `read_only: true`, tmpfs `/tmp`, `/spool` = the `media-spool` tmpfs volume | ADR-002 §2 |
| limits | `mem_limit: 6g`, `cpus: 2`, `pids_limit: 1024` | ADR-002 §2 |

`docker-seccomp.json` provenance: `https://raw.githubusercontent.com/moby/moby/v25.0.8/profiles/seccomp/default.json`
(sha256 `9c1025c88ccaa517b648da571961838744ea2137f176bfe6a48b21294cae9c76`, fetched in a docker build), with the
single rule `{"names": ["pivot_root"], "action": "SCMP_ACT_ALLOW", "includes": {"caps": ["CAP_SYS_ADMIN"]}}` appended.
Regenerate it the same way when the Docker engine is upgraded.

## Ladder

| # | Change from the adopted settings | Selftest | Conclusion |
|---|---|---|---|
| 1 | outer seccomp = Docker default (the ADR target) | exit 2: `pivot_root(...): Operation not permitted` in every jail | the ADR's "default seccomp allows pivot_root with CAP_SYS_ADMIN" does not hold for Docker 25 |
| 2 | adopted settings | exit 0, 16/16 | adopted |
| 3 | outer seccomp = `unconfined` (the ADR fallback) | exit 0 | not needed |
| 4 | without `SYS_ADMIN` | exit 2 (jail cannot start) | required (namespaces, mounts) |
| 5 | without `SETUID` | exit 2 | required (`uidmap` to outside uid 100000+slot) |
| 6 | without `SETGID` | exit 2 | required (`gidmap`) |
| 7 | without `KILL` | exit 0 | nsjail's `time_limit` kill works without it (the jail's user namespace is owned by root); kept until sandboxd's own tests (SEC-SBX-05) show it is unneeded |
| 8 | `CHOWN`, `DAC_OVERRIDE`, `FOWNER` | not relaxed | needed by sandboxd's `out/` ownership walk (ADR-002 §6: `chown` of `out/` to the slot uid, `fchown`/`fchmod` back to 10001:10002, deleting jail-written entries) and by the selftest's own `out/` chown |
| 9 | jail `mount_proc: true` | jail fails unless `systempaths=unconfined` | keep `mount_proc: false`; Docling (`pdf.markdown`) converts `invoice.pdf` inside a jail without `/proc` (only NNPACK "could not initialize" warnings) |

## Selftest results (adopted settings)

| ADR check | Probe | Result |
|---|---|---|
| 1 | `socket(AF_INET)` | `EAFNOSUPPORT` (97); the jailed interpreter loads, so the `/usr`, `/lib`, `/lib64` binds work on merged-`/usr` Debian |
| 2 | interfaces (policy off, namespaces only) | only `lo`, down; `connect(127.0.0.1)` → `ENETUNREACH` |
| 3 | spool isolation | `/work` = `in`, `out`; no `/spool`; a sibling job's file is unreachable |
| 4 | `/proc/1/environ` | unreadable (`ENOENT`: no `/proc` in the jail) |
| 5 | `ptrace`, `io_uring_setup` | SIGSYS (exit 159) |
| 6 | writes to `/`, `/usr`, `/etc`, `/work`, `/work/in`, `/opt/noema-sandbox` | `EROFS`; `/work/out` and `/tmp` writable |
| 7 | 1 GiB allocation under `rlimit_as` 512 MiB | `MemoryError`, exit 1 |
| 8 | busy loop, `time_limit: 2` | SIGKILL (exit 137) after ~2.0 s |
| 9 | `unshare(CLONE_NEWUSER)`, `clone(CLONE_NEWUSER)`, `fsopen`, `bpf` | SIGSYS (exit 159) |
| 9 | `clone3` | `ENOSYS` (38) |
| 9 | `socket(AF_INET6)`, `socket(AF_NETLINK)` | `EAFNOSUPPORT`; `AF_UNIX` allowed |
| 10 | x32 `ptrace` (`0x40000000 \| 521`) | `ENOSYS`: the kernel has no x32 ABI. The kafel policy has no x32 rule, so on an x32-enabled kernel this check fails and the policy must gain x32 kill rules first (ADR-002 §2 check 10) |

## Corrections to the ADR-002 §3 nsjail template (for sandboxd, media-pipeline)

Found while building the selftest jail (`selftest.py: jail_config`), which is a working instance of the template:

1. tmpfs flags are mount fields, not data options: `mount { dst: "/tmp" fstype: "tmpfs" rw: true options: "size=256m" nosuid: true nodev: true }`. The ADR's `options: "size=256m,nosuid,nodev"` fails with `EINVAL`.
2. `rlimit_nproc_type` and `rlimit_stack_type` default to `SOFT` in nsjail 3.6, which ignores `rlimit_nproc`/`rlimit_stack`: set `rlimit_nproc_type: VALUE` and `rlimit_stack_type: VALUE` (the other rlimit types default to `VALUE`).
3. The pinned kafel names x86_64 syscall 166 (`umount2`) `umount`; `seccomp.kafel` writes it as `SYSCALL[166]`. Every other name in the ADR-002 §4 list exists in this kafel.
4. Pass the policy with `--seccomp_policy /etc/noema-sandbox/seccomp.kafel` (or `seccomp_policy_file`); the image sets `NOEMA_SANDBOX_POLICY` to that path.
