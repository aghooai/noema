"""Alert hygiene (deploy/observability/prometheus/rules): kebab-case names that double as runbook anchors, a
severity, the three annotations, and every alert explained in docs/operations/observability.md."""

from __future__ import annotations

import os
import re

import pytest
from tests.unit.observability import sources

NAME = re.compile(r"^noema(-[a-z0-9]+)+$")
SEVERITIES = {"critical", "warning"}
ANNOTATIONS = {"summary", "description", "runbook_url"}
RECORD = re.compile(r"^noema:[a-z0-9_]+:[a-z0-9_]+$")


def test_alerts_are_named_labelled_and_annotated() -> None:
    names = [a["alert"] for a in sources.alerts()]
    assert len(set(names)) == len(names)
    for a in sources.alerts():
        name = a["alert"]
        assert NAME.match(name), name
        assert a["labels"]["severity"] in SEVERITIES, name
        assert set(a["annotations"]) == ANNOTATIONS, name
        assert a["annotations"]["runbook_url"] == f"docs/operations/runbooks.md#{name}", name
        assert all(str(v).strip() for v in a["annotations"].values()), name


def test_recording_rules_follow_the_naming_convention() -> None:
    names = [r["record"] for r in sources.rules() if "record" in r]
    assert len(set(names)) == len(names)
    assert all(RECORD.match(n) for n in names), names


def test_every_alert_is_documented() -> None:
    doc = sources.DOC.read_text()
    missing = [a["alert"] for a in sources.alerts() if f"`{a['alert']}`" not in doc]
    assert not missing, f"not in {sources.DOC.name}: {missing}"


# The runbooks are written by another role in the same wave; `make observability-check` sets the variable, so a
# half-written runbooks.md never fails `make check`.
@pytest.mark.skipif(not os.environ.get("NOEMA_OBS_RUNBOOKS"), reason="make observability-check only")
def test_every_alert_has_a_runbook_anchor() -> None:
    assert sources.RUNBOOKS.exists(), f"{sources.RUNBOOKS} does not exist"
    anchors = sources.markdown_anchors(sources.RUNBOOKS.read_text())
    missing = [a["alert"] for a in sources.alerts() if a["alert"] not in anchors]
    assert not missing, f"no anchor in {sources.RUNBOOKS.name}: {missing}"
