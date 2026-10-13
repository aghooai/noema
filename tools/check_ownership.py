#!/usr/bin/env python3
"""Map changed paths to their owners (OWNERS.yaml) and enforce one agent's ownership.

  check_ownership.py                      changed paths from `git status --porcelain` (read-only), with owners;
                                          exit 1 if any changed path has no owner
  check_ownership.py --role R             exit 1 if any changed path is not owned by role R
  check_ownership.py --paths a b ...      check the given repo-relative paths instead of git status
  check_ownership.py --owner-of PATH      print the owners of one path

The first OWNERS.yaml rule whose pattern matches decides; `*` stays inside a segment, `**` spans segments, `{a,b}`
are alternatives and `<role>` matches a role name that becomes the owner.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parent.parent
OWNERS_FILE = REPO / "OWNERS.yaml"
ROLE_TOKEN = "<role>"  # noqa: S105 (a placeholder, not a secret)
_BRACES = re.compile(r"\{([^{}]*)\}")


def expand_braces(pattern: str) -> list[str]:
    """`a/{b,c}/d` -> `["a/b/d", "a/c/d"]` (repeatedly, left to right)."""
    m = _BRACES.search(pattern)
    if m is None:
        return [pattern]
    out: list[str] = []
    for alt in m.group(1).split(","):
        out.extend(expand_braces(pattern[: m.start()] + alt + pattern[m.end() :]))
    return out


def glob_to_regex(pattern: str) -> str:
    """One brace-free glob as an anchored regex body; `<role>` becomes the named group `role`."""
    out, i = [], 0
    while i < len(pattern):
        if pattern.startswith("**/", i):
            out.append("(?:.*/)?")
            i += 3
        elif pattern.startswith("/**", i) and i + 3 == len(pattern):
            out.append("(?:/.*)?")
            i += 3
        elif pattern.startswith("**", i):
            out.append(".*")
            i += 2
        elif pattern.startswith(ROLE_TOKEN, i):
            out.append("(?P<role>[a-z0-9][a-z0-9-]*)")
            i += len(ROLE_TOKEN)
        elif pattern[i] == "*":
            out.append("[^/]*")
            i += 1
        elif pattern[i] == "?":
            out.append("[^/]")
            i += 1
        else:
            out.append(re.escape(pattern[i]))
            i += 1
    return "".join(out)


@dataclass(frozen=True)
class Rule:
    pattern: str
    owners: tuple[str, ...]
    regexes: tuple[re.Pattern[str], ...]

    def match(self, path: str, roles: frozenset[str]) -> tuple[str, ...] | None:
        """The owners if `path` matches, else None."""
        for rx in self.regexes:
            m = rx.fullmatch(path)
            if m is None:
                continue
            if ROLE_TOKEN in self.owners:
                role = m.group("role")
                if role not in roles:
                    return None
                return tuple(role if o == ROLE_TOKEN else o for o in self.owners)
            return self.owners
        return None


@dataclass(frozen=True)
class Owners:
    roles: frozenset[str]
    rules: tuple[Rule, ...]
    co_owners: tuple[Rule, ...] = ()

    @classmethod
    def load(cls, path: Path = OWNERS_FILE) -> Owners:
        doc = yaml.safe_load(path.read_text())
        roles = frozenset(doc["roles"])

        def parse(raws: Iterable[dict[str, object]]) -> tuple[Rule, ...]:
            rules = []
            for raw in raws:
                pattern, owners = str(raw["pattern"]), tuple(str(o) for o in raw["owners"])  # type: ignore[attr-defined]
                unknown = [o for o in owners if o != ROLE_TOKEN and o not in roles]
                if unknown:
                    raise ValueError(f"OWNERS.yaml: rule {pattern!r} names unknown roles {unknown}")
                regexes = tuple(re.compile(glob_to_regex(p)) for p in expand_braces(pattern))
                rules.append(Rule(pattern, owners, regexes))
            return tuple(rules)

        return cls(roles, parse(doc["rules"]), parse(doc.get("co_owners", ())))

    def owners_of(self, path: str) -> tuple[str, ...]:
        """The owners of a repo-relative POSIX path: the first matching rule's owners plus every matching co-owner
        rule's owners; () if no rule matches."""
        for rule in self.rules:
            owners = rule.match(path, self.roles)
            if owners is not None:
                break
        else:
            return ()
        for co in self.co_owners:
            extra = co.match(path, self.roles)
            if extra:
                owners += tuple(o for o in extra if o not in owners)
        return owners


def changed_paths(repo: Path = REPO) -> list[str]:
    """Paths changed in the working tree and index, including untracked files (git status; read-only)."""
    out = subprocess.run(
        ["git", "status", "--porcelain=v1", "-z", "--untracked-files=all"],
        cwd=repo,
        capture_output=True,
        check=True,
    ).stdout.decode()
    entries = out.split("\0")
    paths: list[str] = []
    i = 0
    while i < len(entries):
        entry = entries[i]
        i += 1
        if len(entry) < 4:
            continue
        status, path = entry[:2], entry[3:]
        paths.append(path)
        if "R" in status or "C" in status:  # renames and copies: the next entry is the source path
            paths.append(entries[i])
            i += 1
    return sorted(set(paths))


def report(owners: Owners, paths: Iterable[str], role: str | None) -> int:
    """Print path -> owners; return the exit status (1 on an unowned path, or a path not owned by `role`)."""
    if role is not None and role not in owners.roles:
        print(f"unknown role {role!r}; roles: {', '.join(sorted(owners.roles))}")
        return 2
    bad: list[str] = []
    for path in paths:
        who = owners.owners_of(path)
        ok = bool(who) and (role is None or role in who)
        print(f"{'ok ' if ok else 'BAD'}  {path}  ->  {', '.join(who) or 'UNOWNED'}")
        if not ok:
            bad.append(path)
    if bad:
        why = f"not owned by {role}" if role else "without an owner"
        print(f"\n{len(bad)} changed path(s) {why}")
        return 1
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--role", help="fail if a changed path is not owned by this role")
    parser.add_argument("--paths", nargs="+", help="repo-relative paths to check instead of git status")
    parser.add_argument("--owner-of", metavar="PATH", help="print the owners of one path and exit")
    parser.add_argument("--owners-file", type=Path, default=OWNERS_FILE)
    args = parser.parse_args(argv)
    owners = Owners.load(args.owners_file)
    if args.owner_of:
        who = owners.owners_of(args.owner_of)
        print(", ".join(who) or "UNOWNED")
        return 0 if who else 1
    paths = args.paths if args.paths is not None else changed_paths()
    if not paths:
        print("no changed paths")
        return 0
    return report(owners, paths, args.role)


if __name__ == "__main__":
    sys.exit(main())
