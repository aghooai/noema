"""SE13 gate over the supply-chain scanner outputs of `make qa-security-sca` (spec-09 §9 SEC-SC-01): pip-audit
(uv.lock export), npm audit (root and sdk/typescript lockfiles), trivy per image. Prints one line per scanner and
exits 1 when a HIGH or CRITICAL finding is not waived in `reports/security/waivers.md` (a table row naming the
vulnerability id with an `expires` date in the future); fixable and unfixable findings are counted apart.

    python -m tests.security.sca_gate <scan dir> [<waivers.md>]
"""

from __future__ import annotations

import json
import re
import sys
from collections import Counter
from datetime import UTC, date, datetime
from pathlib import Path


def waived(path: Path) -> set[str]:
    """Vulnerability ids of waiver rows whose ISO `YYYY-MM-DD` expiry is today or later."""
    if not path.exists():
        return set()
    today = datetime.now(UTC).date()
    ids: set[str] = set()
    for line in path.read_text().splitlines():
        expiry = re.search(r"\b(\d{4}-\d{2}-\d{2})\b", line)
        if expiry and date.fromisoformat(expiry.group(1)) >= today:
            ids |= set(re.findall(r"\b(?:CVE-\d{4}-\d+|GHSA-[\w-]+|PYSEC-\d{4}-\d+)\b", line))
    return ids


def trivy(path: Path, ok: set[str]) -> tuple[Counter[str], list[str]]:
    counts: Counter[str] = Counter()
    open_ids: list[str] = []
    for result in json.loads(path.read_text()).get("Results", []):
        for v in result.get("Vulnerabilities") or []:
            sev = v["Severity"]
            if sev in ("HIGH", "CRITICAL"):
                counts[f"{sev.lower()}_{'fixable' if v.get('FixedVersion') else 'unfixed'}"] += 1
                if v["VulnerabilityID"] not in ok:
                    open_ids.append(v["VulnerabilityID"])
    return counts, sorted(set(open_ids))


def main(argv: list[str]) -> int:
    scans = Path(argv[0])
    ok = waived(Path(argv[1]) if len(argv) > 1 else Path("reports/security/waivers.md"))
    failed = False
    audit = json.loads((scans / "pip-audit.json").read_text())
    vulns = [(d["name"], v["id"]) for d in audit["dependencies"] for v in d.get("vulns", [])]
    skipped = [d["name"] for d in audit["dependencies"] if d.get("skip_reason")]
    print(f"pip-audit: {len(audit['dependencies'])} packages, {len(vulns)} vulnerable, not auditable: {skipped}")
    failed |= any(vid not in ok for _, vid in vulns)
    for name in sorted(scans.glob("npm-audit-*.json")):
        meta = json.loads(name.read_text()).get("metadata", {}).get("vulnerabilities", {})
        print(f"{name.stem}: {meta}")
        failed |= bool(meta.get("high") or meta.get("critical"))
    for name in sorted(scans.glob("trivy-*.json")):
        counts, open_ids = trivy(name, ok)
        print(f"{name.stem}: HIGH/CRITICAL {dict(counts)}; {len(open_ids)} unwaived ids: {' '.join(open_ids[:12])}")
        failed |= bool(open_ids)
    print(f"waived ids: {len(ok)}; SE13 {'FAIL' if failed else 'PASS'}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
