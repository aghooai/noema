#!/usr/bin/env python3
"""Vendor the canonical contracts and generate the derived artifacts.

  sync_contracts.py                     copy docs/noema/contracts into src/noema/contracts/_vendor byte-for-byte
  sync_contracts.py --check             exit 1 on any difference (missing, extra or changed file)
  sync_contracts.py --generate          bundle openapi.yaml (Redocly) and generate _generated/models.py
  sync_contracts.py --check-generated   regenerate into a temp dir and exit 1 if the committed outputs differ

The canonical tree is NOEMA_CONTRACTS_SRC or --source (default /codes/aghooai/docs/noema/contracts). Python
bytecode caches and notebook checkpoints are never vendored or compared.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
DEFAULT_SOURCE = Path(os.environ.get("NOEMA_CONTRACTS_SRC", "/codes/aghooai/docs/noema/contracts"))
CONTRACTS = REPO / "src" / "noema" / "contracts"
VENDOR = CONTRACTS / "_vendor"
BUNDLE = CONTRACTS / "build" / "openapi.bundled.yaml"
MODELS = CONTRACTS / "_generated" / "models.py"
IGNORED_DIRS = frozenset({"__pycache__", ".ipynb_checkpoints", "node_modules"})
IGNORED_SUFFIXES = (".pyc", ".pyo")
REDOCLY = "@redocly/cli@2.62.1"  # spec-10 §7, spec-08 §10.1
DATAMODEL_ARGS = (  # spec-08 §10.4 (verified flags)
    "--input-file-type", "openapi", "--openapi-scopes", "schemas",
    "--output-model-type", "pydantic_v2.BaseModel", "--base-class", "noema.contracts._base.StrictBase",
    "--target-python-version", "3.12", "--use-annotated", "--field-constraints", "--enum-field-as-literal", "all",
    "--extra-fields", "forbid", "--strict-types", "str", "int", "float", "bool", "--use-standard-collections",
    "--use-union-operator", "--use-type-alias", "--use-schema-description", "--disable-timestamp",
    "--use-double-quotes", "--formatters", "builtin",
)  # fmt: skip


def same(a: Path, b: Path) -> bool:
    """Byte equality (no stat-signature cache, unlike filecmp)."""
    return a.read_bytes() == b.read_bytes()


def tree(root: Path) -> set[Path]:
    """Relative paths of every vendorable file under root."""
    out: set[Path] = set()
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in IGNORED_DIRS)
        for name in filenames:
            if not name.endswith(IGNORED_SUFFIXES):
                out.add((Path(dirpath) / name).relative_to(root))
    return out


def differences(source: Path, dest: Path) -> list[str]:
    """Human-readable differences between the canonical tree and the vendored copy."""
    src, dst = tree(source), tree(dest) if dest.exists() else set()
    diffs = [f"missing in _vendor: {p}" for p in sorted(src - dst)]
    diffs += [f"not in canonical tree: {p}" for p in sorted(dst - src)]
    diffs += [f"content differs: {p}" for p in sorted(src & dst) if not same(source / p, dest / p)]
    return diffs


def sync(source: Path, dest: Path) -> int:
    """Make dest an exact copy of source (vendorable files only); returns the number of files written or removed."""
    changed = 0
    src = tree(source)
    for rel in sorted(tree(dest) - src) if dest.exists() else []:
        (dest / rel).unlink()
        changed += 1
    for rel in sorted(src):
        target = dest / rel
        if target.exists() and same(source / rel, target):
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source / rel, target)
        changed += 1
    for dirpath, dirnames, filenames in os.walk(dest, topdown=False):  # drop directories left empty
        if not dirnames and not filenames and Path(dirpath) != dest:
            Path(dirpath).rmdir()
    return changed


def bundle(openapi: Path, out: Path) -> None:
    """Redocly-bundle openapi.yaml with its external JSON Schemas; any warning is an error (spec-08 §12)."""
    out.parent.mkdir(parents=True, exist_ok=True)
    proc = subprocess.run(
        ["npx", "-y", REDOCLY, "bundle", openapi.name, "-o", str(out)],
        cwd=openapi.parent,
        capture_output=True,
        text=True,
        check=False,
    )
    log = proc.stdout + proc.stderr
    if proc.returncode != 0 or "warning" in log.lower():
        raise SystemExit(f"redocly bundle failed (exit {proc.returncode}):\n{log}")


def generate_models(bundled: Path, out: Path) -> None:
    """Strict pydantic v2 server models from the bundle (datamodel-code-generator, pinned in pyproject)."""
    out.parent.mkdir(parents=True, exist_ok=True)
    cmd = [sys.executable, "-m", "datamodel_code_generator", "--input", str(bundled), "--output", str(out)]
    proc = subprocess.run([*cmd, *DATAMODEL_ARGS], capture_output=True, text=True, check=False, cwd=REPO)
    if proc.returncode != 0:
        raise SystemExit(f"datamodel-codegen failed (exit {proc.returncode}):\n{proc.stdout}{proc.stderr}")


def generate(into_bundle: Path, into_models: Path) -> None:
    bundle(VENDOR / "openapi.yaml", into_bundle)
    generate_models(into_bundle, into_models)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--source", type=Path, default=DEFAULT_SOURCE, help="canonical contracts directory")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--check", action="store_true", help="fail if _vendor differs from the canonical tree")
    mode.add_argument("--generate", action="store_true", help="write the bundle and the generated models")
    mode.add_argument("--check-generated", action="store_true", help="fail if the generated outputs are stale")
    args = parser.parse_args(argv)
    source: Path = args.source
    if args.check_generated:
        with tempfile.TemporaryDirectory(prefix="noema-contracts-") as tmp:
            # the builtin formatter reads its line length from the nearest pyproject.toml of the output file
            shutil.copyfile(REPO / "pyproject.toml", Path(tmp) / "pyproject.toml")
            t_bundle, t_models = Path(tmp) / BUNDLE.name, Path(tmp) / MODELS.name
            generate(t_bundle, t_models)
            pairs = ((BUNDLE, t_bundle), (MODELS, t_models))
            stale = [str(p.relative_to(REPO)) for p, t in pairs if not p.exists() or not same(p, t)]
        if stale:
            print("stale generated contract files (run: make contracts-generate):", *stale, sep="\n  ")
            return 1
        print("generated contract files are up to date")
        return 0
    if args.generate:
        generate(BUNDLE, MODELS)
        print(f"wrote {BUNDLE.relative_to(REPO)} and {MODELS.relative_to(REPO)}")
        return 0
    if not source.is_dir():
        print(f"canonical contracts directory not found: {source}")
        return 2
    if args.check:
        diffs = differences(source, VENDOR)
        if diffs:
            print(f"src/noema/contracts/_vendor differs from {source} (run: make contracts-sync):", *diffs, sep="\n  ")
            return 1
        print(f"_vendor is identical to {source} ({len(tree(source))} files)")
        return 0
    changed = sync(source, VENDOR)
    print(f"synced {source} -> {VENDOR.relative_to(REPO)} ({changed} files written or removed)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
