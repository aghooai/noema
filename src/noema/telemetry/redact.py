"""`redact()`: the spec-09 §4 secret and PII patterns (IMPL-029e).

Patterns are compiled once and applied in table order; every match becomes `[REDACTED:<kind>]`. `secret_value`
replaces the exact value of every `SecretStr` the process has loaded (>= 8 characters, via
`core.secret.registered_secrets()`) plus values registered with `Redactor.register_secret` (for example
`AWS_BEARER_TOKEN_BEDROCK`). The PII recognizers are deterministic: email; IBANs passing mod-97; 13-19 digit
payment cards passing Luhn; E.164 and national phone numbers; US SSNs. The validated recognizers (IBAN, card) run
before phone and SSN, so a phone-shaped middle part never hides the rest of a card or IBAN, and both retry shorter
windows when the greedy match fails validation. `redact(redact(x)) == redact(x)`.
"""

from __future__ import annotations

import re
import threading
from collections.abc import Callable
from dataclasses import dataclass
from typing import Final

from noema.core.secret import registered_secrets

_CROCKFORD: Final = "[0-9A-HJKMNP-TV-Z]"


@dataclass(frozen=True, slots=True)
class _Pattern:
    kind: str
    regex: re.Pattern[str]
    replace: Callable[[re.Match[str], str], str] | None = None  # (match, marker) -> replacement; None = marker


def _luhn_ok(text: str) -> bool:
    digits = [int(c) for c in text if c.isdigit()]
    if not 13 <= len(digits) <= 19:
        return False
    total = 0
    for i, d in enumerate(reversed(digits)):
        if i % 2 == 1:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return total % 10 == 0


def _iban_ok(text: str) -> bool:
    s = "".join(text.split()).upper()
    if not 15 <= len(s) <= 34:
        return False
    rearranged = s[4:] + s[:4]
    try:
        number = int("".join(str(int(c, 36)) for c in rearranged))
    except ValueError:
        return False
    return number % 97 == 1


_TABLE: Final[tuple[_Pattern, ...]] = (
    _Pattern("bearer", re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]{8,}")),
    _Pattern("api_key", re.compile(r"\bak_(?:live|test)_" + _CROCKFORD + r"{26}\.[A-Za-z0-9_-]+")),
    _Pattern("operator_key", re.compile(r"\bop_" + _CROCKFORD + r"{26}\.[A-Za-z0-9_-]+")),
    _Pattern("aws_key_id", re.compile(r"\b(?:A3T[A-Z0-9]|AKIA|ASIA|ABIA|ACCA)[A-Z0-9]{16}\b")),
    _Pattern(
        "aws_secret",
        re.compile(r"(?i)(?:aws_secret_access_key|secret_?access_?key)[\"'\s:=]+[A-Za-z0-9/+]{40}"),
    ),
    _Pattern(
        "aws_session_token",
        re.compile(r"(?i)(?:aws_session_token|session_?token|x-amz-security-token)[\"'\s:=]+[A-Za-z0-9/+=%]{100,}"),
    ),
    _Pattern("sigv4_param", re.compile(r"(?i)\bX-Amz-(?:Signature|Credential|Security-Token)=[^&\s\"']+")),
    _Pattern("bedrock_api_key", re.compile(r"\b(?:ABSK[A-Za-z0-9+/=]{20,}|bedrock-api-key-[A-Za-z0-9+/=]{20,})")),
    _Pattern("jwt", re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]*")),
    _Pattern("external_id", re.compile(r"\baghoo-[A-Za-z0-9._-]+-[A-Za-z0-9]{32}\b")),
    _Pattern("dsn_password", re.compile(r"(?i)\b(?:postgres(?:ql)?|rediss?)://[^:@\s/]+:[^@\s]+@")),
)
SECRET_VALUE_KIND: Final = "secret_value"  # noqa: S105 (a pattern kind, not a secret)
_CARD_MIN, _CARD_MAX = 13, 19
_DIGIT_RUN: Final = re.compile(r"(?<![\w-])\d+(?:[ -]\d+)*(?![\w-])")  # digit groups joined by single spaces/hyphens
_DIGIT_GROUP: Final = re.compile(r"\d+")


def _card_replace(match: re.Match[str], marker: str) -> str:
    """Redact every Luhn-valid card inside one digit run. Windows are whole digit groups of 13-19 digits in total;
    from each start group the longest valid window wins and scanning resumes after it, so a card followed or
    preceded by other digit groups (an expiry, a quantity) is still found."""
    run = match.group(0)
    groups = [(g.start(), g.end()) for g in _DIGIT_GROUP.finditer(run)]
    out: list[str] = []
    copied = 0
    i = 0
    while i < len(groups):
        ends: list[int] = []
        count = 0
        for j in range(i, len(groups)):
            count += groups[j][1] - groups[j][0]
            if count > _CARD_MAX:
                break
            if count >= _CARD_MIN:
                ends.append(j)
        hit = next((j for j in reversed(ends) if _luhn_ok(run[groups[i][0] : groups[j][1]])), None)
        if hit is None:
            i += 1
            continue
        out += (run[copied : groups[i][0]], marker)
        copied = groups[hit][1]
        i = hit + 1
    out.append(run[copied:])
    return "".join(out)


def _iban_replace(match: re.Match[str], marker: str) -> str:
    """Redact a mod-97-valid IBAN; when the whole (greedy) print-format match fails, retry the prefixes that end
    at a space, longest first, so a following group of 1-4 characters does not hide the IBAN."""
    text = match.group(0)
    if _iban_ok(text):
        return marker
    for k in sorted((i for i, c in enumerate(text) if c == " "), reverse=True):
        if _iban_ok(text[:k]):
            return marker + text[k:]
    return text


_PII: Final[tuple[_Pattern, ...]] = (  # the validated recognizers (IBAN, card) run before phone and SSN
    _Pattern(
        "pii_email", re.compile(r"(?<![\w.+-])[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}\b")
    ),
    _Pattern(  # print format (groups of 4, last 1-4) or compact; never spans two IBANs
        "pii_iban", re.compile(r"\b[A-Z]{2}\d{2}(?: ?[A-Z0-9]{4}){2,7}(?: ?[A-Z0-9]{1,4})?\b"), _iban_replace
    ),
    _Pattern("pii_card", _DIGIT_RUN, _card_replace),
    _Pattern(
        "pii_phone",
        re.compile(
            r"(?<![\w+.-])\+[1-9]\d{7,14}(?![\w])"  # E.164
            r"|(?<![\w+(.-])(?:\+\d{1,3}[\s.-]?)?\(\d{2,4}\)[\s.-]?\d{3,4}[\s.-]\d{3,4}(?![\w-])"  # (030) 1234 5678
            r"|(?<![\w+.-])\d{3}[\s.-]\d{3}[\s.-]\d{4}(?![\w-])"  # 415 555 0132, 415-555-0132
            r"|(?<![\w+.-])0\d{1,4}[\s-]\d{3,4}[\s-]\d{3,4}(?![\w-])"  # national trunk prefix: 020 7946 0958
        ),
    ),
    _Pattern("pii_ssn", re.compile(r"\b\d{3}-\d{2}-\d{4}\b")),
)
KINDS: Final[tuple[str, ...]] = (*(p.kind for p in _TABLE), SECRET_VALUE_KIND, *(p.kind for p in _PII))
_MIN_SECRET_LEN: Final = 8
MEMO_TEXT_MAX: Final = 256  # `redact_value` memoizes strings up to this length ...
MEMO_SIZE: Final = 4096  # ... and at most this many (cleared when full)


def _apply(pattern: _Pattern, text: str) -> str:
    marker = f"[REDACTED:{pattern.kind}]"
    if pattern.replace is None:
        return pattern.regex.sub(marker, text)
    replace = pattern.replace
    return pattern.regex.sub(lambda m: replace(m, marker), text)


class Redactor:
    """Applies the spec-09 §4 table. Thread-safe; the `secret_value` pattern is rebuilt only when the set of
    registered secret values changes."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._extra: set[str] = set()
        self._extra_version = 0
        self._built_for: tuple[int, int] = (-1, -1)
        self._secret_re: re.Pattern[str] | None = None
        self._memo: dict[str, str] = {}
        self._memo_for: tuple[int, int] = (-1, -1)

    def register_secret(self, value: str) -> None:
        """Also redact this exact value (ignored below 8 characters, which would redact ordinary words)."""
        if len(value) < _MIN_SECRET_LEN:
            return
        with self._lock:
            if value not in self._extra:
                self._extra.add(value)
                self._extra_version += 1

    def _secret_pattern(self) -> re.Pattern[str] | None:
        version, values = registered_secrets()
        with self._lock:
            key = (version, self._extra_version)
            if key != self._built_for:
                all_values = sorted(values | self._extra, key=len, reverse=True)  # longest first
                self._secret_re = re.compile("|".join(map(re.escape, all_values))) if all_values else None
                self._built_for = key
            return self._secret_re

    def redact(self, text: str) -> str:
        """`text` with every secret and PII match replaced by `[REDACTED:<kind>]`."""
        for pattern in _TABLE:
            text = _apply(pattern, text)
        secret_re = self._secret_pattern()
        if secret_re is not None:
            text = secret_re.sub(f"[REDACTED:{SECRET_VALUE_KIND}]", text)
        for pattern in _PII:
            text = _apply(pattern, text)
        return text

    def redact_value(self, text: str) -> str:
        """`redact(text)`, memoized for strings up to `MEMO_TEXT_MAX` characters: span attribute values (tenant,
        route, model and session ids) repeat on every span, and each `redact` runs ~17 regex passes (E sprint B:
        2% of the api's CPU at 50 streams). The memo is dropped whenever the set of secret values changes, so a
        newly registered secret is redacted from the next call on."""
        if len(text) > MEMO_TEXT_MAX:
            return self.redact(text)
        self._secret_pattern()  # moves `_built_for` when a secret was registered or dropped
        if self._memo_for != self._built_for:
            self._memo.clear()
            self._memo_for = self._built_for
        out = self._memo.get(text)
        if out is None:
            out = self.redact(text)
            if len(self._memo) >= MEMO_SIZE:
                self._memo.clear()
            self._memo[text] = out
        return out


_DEFAULT: Final = Redactor()


def redact(text: str) -> str:
    """Redact with the process-wide redactor (spec-09 §4)."""
    return _DEFAULT.redact(text)


def default_redactor() -> Redactor:
    """The process-wide redactor (the one `redact()` and the log handler of `configure_logging` use)."""
    return _DEFAULT


def register_secret(value: str) -> None:
    """Register a secret value that is not wrapped in `SecretStr` with the process-wide redactor."""
    _DEFAULT.register_secret(value)
