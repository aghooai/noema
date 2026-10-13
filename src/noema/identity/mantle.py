"""Mantle authentication material (ADR-001 §6): the short-term Bedrock bearer token.

`bearer_short_term` (the Phase 1 default) presigns a token offline from the tenant's assumed-role credentials with
the pinned `aws-bedrock-token-generator` (no network call), valid until `min(creds.expires_at, now + 12 h)`.
`sigv4` carries no token: the adapter signs with the AWS credentials. The token is a `SecretStr` registered with the
redactor and is never logged.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Final

from aws_bedrock_token_generator import provide_token
from botocore.credentials import CredentialProvider, Credentials

from noema.core.protocols.credentials import AwsCredentials, MantleAuth
from noema.core.secret import SecretStr

BEARER_MAX_S: Final = 12 * 3600
SIGV4: Final = MantleAuth(mode="sigv4", bearer_token=None, expires_at=None)


class _Static(CredentialProvider):
    """A botocore credential provider over fixed credentials (never the default chain)."""

    METHOD = "noema-broker"

    def __init__(self, creds: AwsCredentials) -> None:
        super().__init__()
        self._creds = creds

    def load(self) -> Credentials:
        token = self._creds.session_token
        return Credentials(
            self._creds.access_key_id,
            self._creds.secret_access_key.get_secret_value(),
            None if token is None else token.get_secret_value(),
            method=self.METHOD,
        )


def short_term_bearer(creds: AwsCredentials, region: str, *, now: datetime, expires_at: datetime | None) -> MantleAuth:
    """A bearer `MantleAuth` presigned from `creds` for `region`; it expires no later than the credentials."""
    limit = now + timedelta(seconds=BEARER_MAX_S)
    until = limit if expires_at is None else min(expires_at, limit)
    seconds = max(1, int((until - now).total_seconds()))
    token = provide_token(region=region, aws_credentials_provider=_Static(creds), expiry=timedelta(seconds=seconds))
    return MantleAuth(
        mode="bearer", bearer_token=SecretStr(token, register=True), expires_at=now + timedelta(seconds=seconds)
    )
