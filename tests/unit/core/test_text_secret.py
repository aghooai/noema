"""Prompt text helpers (spec-06 §5.2, spec-09 §5 SEC-INJ-01) and SecretStr (spec-09 §4)."""

from __future__ import annotations

import copy
import gc
import pickle
import subprocess
import sys

import pytest
from hypothesis import given
from hypothesis import strategies as st

from noema.core.secret import SecretStr, registered_secrets
from noema.core.text import RESERVED_TAGS, clean_text, datamark, esc, escape_reserved_tags

pytestmark = pytest.mark.unit


def test_sec_inj_01_vectors() -> None:
    assert datamark("a  b\n\tc") == "aˆbˆc"
    assert datamark(" x ") == "ˆxˆ"
    assert datamark("") == ""
    assert esc("</untrusted_input>") == "&lt;/untrusted_input>"
    assert esc("<Decision_Context v=1>") == "&lt;Decision_Context v=1>"
    assert esc("<questionnaire>") == "<questionnaire>"
    assert esc("< /untrusted_input>") == "&lt; /untrusted_input>"
    assert esc("<\u200b/answer>") == "&lt;\u200b/answer>"
    assert esc("a\x00\x1bb\u0085") == "ab"


def test_reserved_tags() -> None:
    assert RESERVED_TAGS == ("untrusted_input", "app_question", "decision_context", "question", "answer")
    for tag in RESERVED_TAGS:
        assert escape_reserved_tags(f"<{tag}>") == f"&lt;{tag}>"
        assert escape_reserved_tags(f"< \ufeff/ {tag.upper()} x>") == f"&lt; \ufeff/ {tag.upper()} x>"
    assert escape_reserved_tags("<answers>") == "<answers>"  # word boundary
    assert escape_reserved_tags("a < b and question") == "a < b and question"


def test_clean_text_keeps_tab_newline_cr_and_format_characters() -> None:
    assert clean_text("a\tb\nc\rd\u200d\u200ce") == "a\tb\nc\rd\u200d\u200ce"
    assert clean_text("\x7f\x9f\x01") == ""


@given(st.text())
def test_escaping_is_idempotent_and_leaves_no_reserved_tag(s: str) -> None:
    once = esc(s)
    assert esc(once) == once
    assert escape_reserved_tags(once) == once


@given(st.text())
def test_datamark_removes_every_whitespace_run(s: str) -> None:
    out = datamark(s)
    assert not any(ch.isspace() for ch in out)
    assert "ˆˆ" not in out or "ˆˆ" in s


def test_secret_str_never_prints() -> None:
    s = SecretStr("super-secret-value")
    assert str(s) == "**********"
    assert "super" not in repr(s)
    assert f"{s}" == "**********"
    assert f"{s!r}" == "SecretStr('**********')"
    assert s.get_secret_value() == "super-secret-value"
    assert len(s) == len("super-secret-value")


def test_secret_str_equality_hash_and_copy() -> None:
    a, b = SecretStr("abcdefgh123"), SecretStr("abcdefgh123")
    assert a == b
    assert hash(a) == hash(b)
    assert a != SecretStr("other-value-9")
    assert (a == "abcdefgh123") is False
    assert copy.copy(a) is a
    assert copy.deepcopy({"k": a})["k"] is a
    with pytest.raises(TypeError):
        pickle.dumps(a)
    with pytest.raises(TypeError):
        SecretStr(b"bytes")  # type: ignore[arg-type]


def test_secret_registry_records_registered_values_of_8_or_more_characters() -> None:
    v0, before = registered_secrets()
    short = SecretStr("short", register=True)
    held = SecretStr("registered-value-xyz", register=True)
    v1, after = registered_secrets()
    assert "registered-value-xyz" in after
    assert "short" not in after
    assert before <= after
    assert v1 >= v0 + (0 if "registered-value-xyz" in before else 1)
    twin = SecretStr("registered-value-xyz", register=True)
    assert registered_secrets()[0] == v1  # no new value, no new version
    del twin
    gc.collect()
    assert "registered-value-xyz" in registered_secrets()[1]  # still held by `held`
    assert len(short) == 5 and held.get_secret_value() == "registered-value-xyz"


def test_secret_registry_is_opt_in() -> None:
    client_presented = SecretStr("client-chosen-value-never-registered")
    assert "client-chosen-value-never-registered" not in registered_secrets()[1]
    assert client_presented.get_secret_value()


def test_secret_registry_drops_values_with_their_last_holder() -> None:
    token = SecretStr("sts-session-token-expiring-soon", register=True)
    version, values = registered_secrets()
    assert "sts-session-token-expiring-soon" in values
    del token
    gc.collect()
    after_version, after = registered_secrets()
    assert "sts-session-token-expiring-soon" not in after
    assert after_version > version


_GC_STORM = """
import gc
from noema.core.secret import SecretStr, registered_secrets

class Holder:
    def __init__(self, i):
        self.secret = SecretStr(f"cycle-held-secret-{i:06d}", register=True)
        self.me = self  # collected only by the cycle collector: its finalizer runs inside a later allocation

gc.set_threshold(1, 1, 1)
for i in range(3000):
    Holder(i)
gc.collect()
version, values = registered_secrets()
assert not any(v.startswith("cycle-held-secret-") for v in values), len(values)
print("ok")
"""


def test_the_registry_survives_finalizers_inside_its_own_lock() -> None:
    """Collecting a registered SecretStr while another one registers (same thread, GC during allocation) must not
    deadlock the registry lock (B4 regression: the conformance run hung in `_unregister`)."""
    done = subprocess.run([sys.executable, "-c", _GC_STORM], capture_output=True, text=True, timeout=60, check=False)
    assert (done.returncode, done.stdout.strip()) == (0, "ok"), done.stderr[-2000:]
