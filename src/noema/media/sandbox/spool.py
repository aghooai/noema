"""The spool: one directory per sandbox job, the only channel between media-worker and media-sandbox (ADR-002 §1,
§6). Stdlib only.

Layout (created by the media-worker, uid 10001): `<spool>/<job_id>/` 0711, `in/` 0755, `in/input.bin` 0644, `out/`
0700. Around a jail run, sandboxd (root) hands `out/` to the slot uid and afterwards takes it back with
`reclaim_out`, which never follows a jail-planted link.
"""

from __future__ import annotations

import contextlib
import os
import re
import shutil
import stat
import time
from pathlib import Path
from typing import Final

JOB_ID_RE: Final = re.compile(r"sbx_[0-9A-HJKMNP-TV-Z]{26}")
INPUT_NAME: Final = "input.bin"
WORKER_UID: Final = 10001
WORKER_GID: Final = 10002
ORPHAN_AGE_S: Final = 3600.0


def job_dir(spool_root: Path, job_id: str) -> Path:
    """`<spool>/<job_id>`; ValueError for an id that is not `sbx_<ULID>`."""
    if not JOB_ID_RE.fullmatch(job_id):
        raise ValueError("invalid sandbox job id")
    return spool_root / job_id


def input_path(spool_root: Path, job_id: str) -> Path:
    """The canonical input file of a job: `<spool>/<job_id>/in/input.bin`."""
    return job_dir(spool_root, job_id) / "in" / INPUT_NAME


def prepare(spool_root: Path, job_id: str, source: Path | bytes | None = None, *, link: bool = False) -> Path:
    """Create the job directory with the ADR-002 §6 modes and place the input: `source` bytes are written, a path
    other than the canonical one is copied (`link`: hard-linked when the filesystem allows, for a spool file the
    caller owns), None means the input is already in place. Returns the job dir."""
    jd = job_dir(spool_root, job_id)
    (jd / "in").mkdir(parents=True, exist_ok=True)
    (jd / "out").mkdir(exist_ok=True)
    target = jd / "in" / INPUT_NAME
    if source is not None and (isinstance(source, bytes) or os.path.abspath(source) != os.path.abspath(target)):
        _write_input(target, source, link=link)
    os.chmod(jd, 0o711)  # noqa: S103 (ADR-002 §6: jails traverse the job dir, read in/)
    os.chmod(jd / "in", 0o755)  # noqa: S103
    os.chmod(target, 0o644)
    os.chmod(jd / "out", 0o700)
    return jd


def _write_input(target: Path, source: Path | bytes, *, link: bool) -> None:
    """(Re)create `in/input.bin` without ever following a link at the target name."""
    target.unlink(missing_ok=True)
    if link and not isinstance(source, bytes):
        try:
            os.link(source, target, follow_symlinks=False)  # one copy of a large input in the tmpfs, not one per job
            return
        except OSError:  # another filesystem, or protected_hardlinks: copy
            pass
    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o644)
    with os.fdopen(fd, "wb") as out:
        if isinstance(source, bytes):
            out.write(source)
        else:
            with Path(source).open("rb") as src:
                shutil.copyfileobj(src, out, 1024 * 1024)


def hand_out_to(out_dir: Path, uid: int, gid: int) -> None:
    """Give `out/` (the directory only) to a jail slot uid (sandboxd, root)."""
    fd = os.open(out_dir, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        os.fchown(fd, uid, gid)
        os.fchmod(fd, 0o700)
    finally:
        os.close(fd)


def _remove_entry(entry: os.DirEntry[str]) -> None:
    if entry.is_dir(follow_symlinks=False):
        shutil.rmtree(entry.path, ignore_errors=True)  # fd-based rmtree: never follows links inside
    else:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(entry.path)


def reclaim_out(out_dir: Path, *, uid: int | None, gid: int | None) -> list[str]:
    """ADR-002 §6 ownership walk after a jail run: delete every entry of `out/` that is not a regular file with
    exactly one link (symlinks, directories, FIFOs, devices, hard links), then `fchown`/`fchmod 0640` each remaining
    file through an `O_NOFOLLOW` fd and give `out/` back (0750). `uid`/`gid` None skips the chown (backends that
    run jails as the caller). Never calls a path-based chown/chmod on a jail-written name. Returns the kept names."""
    kept: list[str] = []
    with os.scandir(out_dir) as it:  # decide on the state the jail left (both names of a hard link go)
        entries = [(entry, entry.stat(follow_symlinks=False)) for entry in it]
    for entry, st in entries:
        if not stat.S_ISREG(st.st_mode) or st.st_nlink != 1:
            _remove_entry(entry)
            continue
        try:
            fd = os.open(entry.path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
        except OSError:
            _remove_entry(entry)
            continue
        try:
            fst = os.fstat(fd)
            if not stat.S_ISREG(fst.st_mode) or (fst.st_dev, fst.st_ino) != (st.st_dev, st.st_ino):
                os.unlink(entry.path)
                continue
            if uid is not None and gid is not None:
                os.fchown(fd, uid, gid)
            os.fchmod(fd, 0o640)
            kept.append(entry.name)
        finally:
            os.close(fd)
    fd = os.open(out_dir, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        if uid is not None and gid is not None:
            os.fchown(fd, uid, gid)
        os.fchmod(fd, 0o750)
    finally:
        os.close(fd)
    return sorted(kept)


def remove(spool_root: Path, job_id: str) -> None:
    """Delete a job directory (after its outputs were stored, on success or failure)."""
    with contextlib.suppress(ValueError):
        shutil.rmtree(job_dir(spool_root, job_id), ignore_errors=True)


def sweep_orphans(spool_root: Path, *, older_than_s: float = ORPHAN_AGE_S, now: float | None = None) -> int:
    """Delete job directories untouched for `older_than_s` (sandboxd, hourly); returns how many."""
    cutoff = (time.time() if now is None else now) - older_than_s
    removed = 0
    with os.scandir(spool_root) as it:
        for entry in it:
            stale = JOB_ID_RE.fullmatch(entry.name) and entry.is_dir(follow_symlinks=False)
            if stale and entry.stat(follow_symlinks=False).st_mtime < cutoff:
                shutil.rmtree(entry.path, ignore_errors=True)
                removed += 1
    return removed
