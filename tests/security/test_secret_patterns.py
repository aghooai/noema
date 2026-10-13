"""The scanner's own patterns (tests/security/secret_patterns.py): spec-09 §4 positive vectors match, the SEC-RED-2
negatives (a bare ULID, a sha256 hex digest, a `dec_` id, a `doc-1` label) and redaction placeholders do not."""

from __future__ import annotations

import pytest
from tests.security.secret_patterns import scan

pytestmark = [pytest.mark.unit]
ULID = "01M4MW5D1D4HEK368WMZXN70B4"
POSITIVE = {
    "bearer": "Authorization: Bearer abcdefgh12345678",
    "api_key": f"ak_test_{ULID}.short",
    "operator_key": f"op_{ULID}.x",
    "aws_key_id": "ASIAABCDEFGHIJKLMNOP",
    "aws_secret": "aws_secret_access_key=" + "A" * 40,
    "aws_session_token": "x-amz-security-token: " + "B" * 120,
    "sigv4_param": "X-Amz-Signature=deadbeef",
    "bedrock_api_key": "bedrock-api-key-" + "C" * 24,
    "jwt": "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTYifQ.sig",
    "external_id": "aghoo-t_acme-" + "D" * 32,
    "dsn_password": "postgresql://noema_app:pw@host/db",
}


@pytest.mark.parametrize(("kind", "text"), sorted(POSITIVE.items()))
def test_each_pattern_matches_its_vector(kind: str, text: str) -> None:
    assert kind in {f.kind for f in scan("t", text, {})}


@pytest.mark.parametrize(
    "text",
    [ULID, "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855", "dec_01M4MWYGS9MWF1T86S4D1T03AJ",
     "doc-1", "[REDACTED:api_key] [REDACTED:bearer]", "aws-sm://platform/tenants/t_acme/external-id"],
)  # fmt: skip
def test_negatives_do_not_match(text: str) -> None:
    assert scan("t", text, {}) == []


def test_known_values_are_found_and_masked() -> None:
    (finding,) = scan("t", "x pepper-value-123456 y", {"pepper": "pepper-value-123456"})
    assert finding.kind == "value:pepper" and "pepper-value-123456" not in finding.excerpt
