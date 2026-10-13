"""The spec-09 §4 secret patterns, written again from the spec table (never imported from `noema.telemetry`), and a
scanner that reports every match outside a `[REDACTED:<kind>]` placeholder, plus every exact occurrence of a known
secret value (API and operator keys and their secret halves, peppers, ExternalIds, DSN passwords)."""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Final

PATTERNS: Final[dict[str, re.Pattern[str]]] = {
    "bearer": re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]{8,}"),
    "api_key": re.compile(r"\bak_(?:live|test)_[0-9A-HJKMNP-TV-Z]{26}\.[A-Za-z0-9_-]+"),
    "operator_key": re.compile(r"\bop_[0-9A-HJKMNP-TV-Z]{26}\.[A-Za-z0-9_-]+"),
    "aws_key_id": re.compile(r"\b(?:A3T[A-Z0-9]|AKIA|ASIA|ABIA|ACCA)[A-Z0-9]{16}\b"),
    "aws_secret": re.compile(r"(?i)(?:aws_secret_access_key|secret_?access_?key)[\"'\s:=]+[A-Za-z0-9/+]{40}"),
    "aws_session_token": re.compile(
        r"(?i)(?:aws_session_token|session_?token|x-amz-security-token)[\"'\s:=]+[A-Za-z0-9/+=%]{100,}"
    ),
    "sigv4_param": re.compile(r"(?i)\bX-Amz-(?:Signature|Credential|Security-Token)=[^&\s\"']+"),
    "bedrock_api_key": re.compile(r"\b(?:ABSK[A-Za-z0-9+/=]{20,}|bedrock-api-key-[A-Za-z0-9+/=]{20,})"),
    "jwt": re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]*"),
    "external_id": re.compile(r"\baghoo-[A-Za-z0-9._-]+-[A-Za-z0-9]{32}\b"),
    "dsn_password": re.compile(r"(?i)\b(?:postgres(?:ql)?|rediss?)://[^:@\s/]+:[^@\s]+@"),
}


@dataclass(frozen=True)
class Finding:
    source: str
    line: int
    kind: str
    excerpt: str  # the match with its middle masked (the report never repeats a secret)


def mask(value: str) -> str:
    return value if len(value) <= 8 else f"{value[:4]}…{value[-2:]}({len(value)})"


def scan(
    source: str,
    text: str,
    values: Mapping[str, str],
    *,
    kinds: Iterable[str] = tuple(PATTERNS),
    allow: Iterable[str] = (),
) -> list[Finding]:
    """Every pattern match (of `kinds`) and every occurrence of a known value (`values`: label -> secret) in `text`.
    `allow`: exact match strings that are not findings (e.g. a fake fixture credential documented as such)."""
    allowed = set(allow)
    findings: list[Finding] = []
    for number, line in enumerate(text.splitlines(), 1):
        for kind in kinds:
            hits = [m.group(0) for m in PATTERNS[kind].finditer(line)]
            findings.extend(Finding(source, number, kind, mask(h)) for h in hits if h not in allowed)
        for label, value in values.items():
            if value and value in line:
                findings.append(Finding(source, number, f"value:{label}", mask(value)))
    return findings
