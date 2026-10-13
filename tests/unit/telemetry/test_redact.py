"""SEC-RED-2 (spec-09 §4): every pattern has positive and negative vectors; nothing secret survives; redaction
is idempotent; negatives (ULIDs, sha256 hex, `dec_…` ids, `doc-1` labels) are left alone."""

from __future__ import annotations

import base64
import gc
import re

import pytest
from hypothesis import example, given, settings
from hypothesis import strategies as st

from noema.core.secret import SecretStr
from noema.telemetry.redact import KINDS, MEMO_SIZE, MEMO_TEXT_MAX, Redactor, redact

ULID = "01J9ZQ7ABCDEFGHJKMNPQRSTVW"
SHA = "734dae2685c4f2a89e350ac3cb8a74f58f9c67d32264a5cee3cd87eebce9455a"
TOKEN100 = "A" * 60 + "b" * 40 + "%2F"
POSITIVE = [
    ("bearer", "Authorization: Bearer abcdefgh.ijk-lmn_op", "abcdefgh.ijk-lmn_op"),
    ("api_key", f"key ak_live_{ULID}.s3cr3t-Part_x9", "s3cr3t-Part_x9"),
    ("api_key", f"truncated ak_test_{ULID}.x", f"ak_test_{ULID}.x"),
    ("operator_key", f"op_{ULID}.Zz9_operator", "Zz9_operator"),
    ("aws_key_id", "creds AKIAIOSFODNN7EXAMPLE used", "AKIAIOSFODNN7EXAMPLE"),
    ("aws_key_id", "ASIAQWERTYUIOPASDFGH", "ASIAQWERTYUIOPASDFGH"),
    ("aws_secret", "aws_secret_access_key = wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY", "wJalrXUtnFEMI/K7MDENG"),
    ("aws_session_token", f"X-Amz-Security-Token: {TOKEN100}", TOKEN100),
    ("sigv4_param", "https://s3/x?X-Amz-Signature=abc123def456&y=1", "abc123def456"),
    ("sigv4_param", "?X-Amz-Credential=AKIDEXAMPLE%2F20261010%2Fus-east-1", "AKIDEXAMPLE%2F2026"),
    ("bedrock_api_key", "ABSKQmVkcm9ja0FQSUtleS1leGFtcGxlMTIzNDU2", "ABSKQmVkcm9ja0FQSUtleS1"),
    ("bedrock_api_key", "bedrock-api-key-YmVkcm9jay5hbWF6b25hd3MuY29t", "YmVkcm9jay5hbWF6b25hd3MuY29t"),
    ("jwt", "t=eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0In0.c2lnbmF0dXJl", "eyJzdWIiOiIxMjM0In0"),
    ("external_id", "aghoo-t_acme-0123456789abcdef0123456789abcdef", "0123456789abcdef0123456789abcdef"),
    ("dsn_password", "postgresql://noema_app:hunter2-pw@db:5432/noema", "hunter2-pw"),
    ("dsn_password", "rediss://default:p4ss@cache:6380/0", "p4ss"),
    ("pii_email", "mail jane.doe+tag@example.co.uk now", "jane.doe+tag@example.co.uk"),
    ("pii_phone", "call +14155550132 today", "+14155550132"),
    ("pii_phone", "call (030) 1234 5678", "1234 5678"),
    ("pii_phone", "call 415-555-0132", "415-555-0132"),
    ("pii_phone", "call 020 7946 0958", "7946 0958"),
    ("pii_card", "card 4111 1111 1111 1111 exp", "4111 1111 1111 1111"),
    ("pii_card", "card 5500-0000-0000-0004", "5500-0000-0000-0004"),
    ("pii_iban", "IBAN GB82 WEST 1234 5698 7654 32", "WEST 1234 5698"),
    ("pii_iban", "DE89370400440532013000", "DE89370400440532013000"),
    ("pii_ssn", "ssn 078-05-1120", "078-05-1120"),
]
NEGATIVE = [
    ULID,
    SHA,
    f"dec_{ULID}",
    f"turn_{ULID}",
    "doc-1",
    "image-2",
    "2026-10-10T12:00:00Z",
    "version 1.43.106",
    "4111 1111 1111 1112",  # fails Luhn
    "GB82 WEST 1234 5698 7654 33",  # fails mod-97
    "bearer_short_term",
    "count 1234567",
    "port 25432",
    "sha256:" + SHA,
    "us.anthropic.claude-opus-5-5",
    "x-ratelimit-remaining-tokens: 19999000",
    "the year 2026",
    "price 0.000105",
]


@pytest.mark.parametrize(("kind", "text", "secret"), POSITIVE, ids=[f"{k}:{i}" for i, (k, _, _) in enumerate(POSITIVE)])
def test_positive_vectors(kind: str, text: str, secret: str) -> None:
    out = redact(text)
    assert f"[REDACTED:{kind}]" in out, out
    assert secret not in out


@pytest.mark.parametrize("text", NEGATIVE)
def test_negative_vectors(text: str) -> None:
    assert redact(text) == text


def test_every_kind_has_a_positive_vector() -> None:
    covered = {k for k, _, _ in POSITIVE} | {"secret_value"}
    assert covered == set(KINDS)


def test_secret_values_are_redacted_exactly() -> None:
    value = "Zm9vYmFyLXNlY3JldC12YWx1ZS0xMjM="
    held = SecretStr(value, register=True)
    assert redact(f"leak {value} here") == "leak [REDACTED:secret_value] here"
    unregistered = SecretStr("dGhpcy1pcy1hLWNsaWVudC12YWx1ZQ==")  # client-presented: never registered
    assert redact(f"x {unregistered.get_secret_value()}") == f"x {unregistered.get_secret_value()}"
    del held
    gc.collect()
    assert redact(f"leak {value} here") == f"leak {value} here"  # dropped with its last holder
    short = Redactor()
    short.register_secret("tiny")  # below 8 characters: ignored (would redact words)
    assert short.redact("a tiny word") == "a tiny word"
    extra = Redactor()
    bearer_env = base64.b64encode(b"long-term-bedrock-key-material").decode()
    extra.register_secret(bearer_env)
    assert bearer_env not in extra.redact(f"AWS_BEARER_TOKEN_BEDROCK={bearer_env}")


def test_idempotent_on_the_vectors() -> None:
    for _, text, _ in POSITIVE:
        once = redact(text)
        assert redact(once) == once


_ALPHABET = st.characters(codec="utf-8", categories=("L", "N", "P", "S", "Zs"))


# Layouts that leaked before the validated recognizers ran first and retried shorter windows (B3 verifier).
LEAK_REGRESSIONS = [
    ("card 4111 1111 1111 1111 12/27", "card [REDACTED:pii_card] 12/27"),
    ("order 4111111111111111 2 units", "order [REDACTED:pii_card] 2 units"),
    ("card 5500-0000-0000-0004 0", "card [REDACTED:pii_card] 0"),
    ("pay 5500 0000 0000 0004 12", "pay [REDACTED:pii_card] 12"),
    ("iban DE89 3704 0044 0532 0130 00 ok", "iban [REDACTED:pii_iban] ok"),
    ("HU42 1177 3016 1111 1018 0000 0000 1234 ok", "[REDACTED:pii_iban] 1234 ok"),  # 28 chars + a 4-char group
    ("12 4111111111111111", "12 [REDACTED:pii_card]"),
    ("4111-1111-1111-1111-5", "[REDACTED:pii_card]-5"),
]


@pytest.mark.parametrize(("text", "expected"), LEAK_REGRESSIONS)
def test_cards_and_ibans_next_to_other_digit_groups(text: str, expected: str) -> None:
    assert redact(text) == expected
    assert redact(expected) == expected


_CARDS = ("4111111111111111", "5500000000000004", "378282246310005", "6011111111111117", "4222222222222")


@settings(max_examples=200, deadline=None)
@given(
    st.sampled_from(_CARDS),
    st.sampled_from([" ", "-", ""]),
    st.lists(st.text("0123456789", min_size=1, max_size=4), max_size=3),
    st.lists(st.text("0123456789", min_size=1, max_size=4), max_size=3),
)
def test_a_card_between_digit_groups_never_survives(card: str, sep: str, before: list[str], after: list[str]) -> None:
    printed = sep.join(card[i : i + 4] for i in range(0, len(card), 4)) if sep else card
    text = " ".join([*before, printed, *after])
    once = redact(text)
    assert printed not in once
    assert redact(once) == once


@settings(max_examples=300, deadline=None)
@example(parts=["card 5500-0000-0000-0004", "0"])  # the shrunk failure of the greedy card regex
@example(parts=["card 4111 1111 1111 1111 exp", "12/27"])
@example(parts=["IBAN GB82 WEST 1234 5698 7654 32", "1234"])
@example(parts=["DE89370400440532013000", "5"])
@given(
    st.lists(
        st.one_of(st.text(_ALPHABET, max_size=20), st.sampled_from([t for _, t, _ in POSITIVE] + NEGATIVE)), max_size=6
    )
)
def test_idempotent_property(parts: list[str]) -> None:
    text = " ".join(parts)
    once = redact(text)
    assert redact(once) == once
    for _, raw, secret in POSITIVE:
        if raw in text:
            assert secret not in once or re.search(re.escape(secret), text.replace(raw, "")) is not None


@pytest.mark.parametrize(("kind", "text", "secret"), POSITIVE)
def test_redact_value_equals_redact_on_the_vectors(kind: str, text: str, secret: str) -> None:
    memo = Redactor()
    assert memo.redact_value(text) == memo.redact(text)
    assert memo.redact_value(text) == memo.redact(text)  # the memoized answer


def test_redact_value_forgets_its_memo_when_a_secret_is_registered() -> None:
    memo = Redactor()
    value = "tenant-material-0123456789"
    assert memo.redact_value(f"t {value}") == f"t {value}"  # memoized before the value was a secret
    held = SecretStr(value, register=True)
    assert memo.redact_value(f"t {value}") == "t [REDACTED:secret_value]"
    memo.register_secret("another-secret-value")
    assert memo.redact_value("x another-secret-value") == "x [REDACTED:secret_value]"
    del held


def test_redact_value_does_not_memoize_long_text_and_stays_bounded() -> None:
    memo = Redactor()
    long_text = "x" * (MEMO_TEXT_MAX + 1) + " ak_live_" + ULID + ".s3cr3t"
    assert memo.redact_value(long_text) == memo.redact(long_text)
    assert long_text not in memo._memo
    for i in range(MEMO_SIZE + 10):
        memo.redact_value(f"id-{i}")
    assert len(memo._memo) <= MEMO_SIZE
