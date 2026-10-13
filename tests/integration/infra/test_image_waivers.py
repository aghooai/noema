"""Defect 29 (SE13): the image CVE waivers in deploy/docker/.trivyignore.yaml.

trivy reads the file as its --ignorefile (`make image-scan`, the CRITICAL gate) and the SE13 summary
(`python -m tests.security.sca_gate <scan dir> deploy/docker/.trivyignore.yaml`) reads it line by line, so every entry
keeps its id, review date and statement on one line. A waiver only counts until its review date.
"""

from __future__ import annotations

import re
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
import yaml
from tests.security import sca_gate

pytestmark = pytest.mark.unit

WAIVERS = Path(__file__).resolve().parents[3] / "deploy" / "docker" / ".trivyignore.yaml"
VULN_ID = re.compile(r"\b(?:CVE-\d{4}-\d+|GHSA-[\w-]+|PYSEC-\d{4}-\d+)\b")
ISO_DATE = re.compile(r"\b\d{4}-\d{2}-\d{2}\b")


def entries() -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = yaml.safe_load(WAIVERS.read_text())["vulnerabilities"]
    return found


def test_every_waiver_names_one_cve_with_a_review_date_and_a_justification() -> None:
    waivers = entries()
    ids = [w["id"] for w in waivers]
    assert waivers and len(ids) == len(set(ids))
    for w in waivers:
        assert set(w) == {"id", "expired_at", "statement"}, w
        assert isinstance(w["expired_at"], date) and len(w["statement"]) > 60, w
        assert VULN_ID.findall(w["statement"]) == [] and ISO_DATE.findall(w["statement"]) == [], w["id"]
    lines = [line for line in WAIVERS.read_text().splitlines() if line.lstrip().startswith("- {")]
    assert [VULN_ID.findall(line) for line in lines] == [[i] for i in ids]
    assert [ISO_DATE.findall(line)[:1] for line in lines] == [[w["expired_at"].isoformat()] for w in waivers]


def _today(monkeypatch: pytest.MonkeyPatch, day: date) -> None:
    class Clock(datetime):
        @classmethod
        def now(cls, tz: Any = None) -> Clock:
            return cls(day.year, day.month, day.day, tzinfo=UTC)

    monkeypatch.setattr(sca_gate, "datetime", Clock)


def test_the_se13_summary_reads_every_waiver_until_its_review_date(monkeypatch: pytest.MonkeyPatch) -> None:
    waivers = entries()
    _today(monkeypatch, min(w["expired_at"] for w in waivers) - timedelta(days=1))
    assert sca_gate.waived(WAIVERS) == {w["id"] for w in waivers}
    _today(monkeypatch, max(w["expired_at"] for w in waivers) + timedelta(days=1))
    assert sca_gate.waived(WAIVERS) == set()
