"""`trust_enforcer` (ADR-001 "How tests simulate cross-account access with moto").

Moto's `sts.assume_role` evaluates neither trust policies nor `ExternalId`. `trust_enforcer(client, caller_arn=…)`
registers a handler on an (aio)botocore STS client that runs before every `AssumeRole` request: it loads the target
role's trust policy (default: from the in-process moto IAM backend), and raises
`ClientError({"Error": {"Code": "AccessDenied"}}, "AssumeRole")` unless an `Allow` statement for `sts:AssumeRole`
lists `caller_arn` in `Principal.AWS` and its `StringEquals sts:ExternalId` (if any) equals the request's.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from typing import Any

from botocore.exceptions import ClientError

PolicyLookup = Callable[[str], Mapping[str, Any] | None]


def _as_list(value: object) -> list[object]:
    return value if isinstance(value, list) else [value]


def trust_allows(policy: Mapping[str, Any], caller_arn: str, external_id: str | None) -> bool:
    """True iff some Allow statement grants `sts:AssumeRole` to `caller_arn` with a matching ExternalId."""
    for stmt in _as_list(policy.get("Statement", [])):
        if not isinstance(stmt, Mapping) or stmt.get("Effect") != "Allow":
            continue
        actions = [str(a) for a in _as_list(stmt.get("Action", []))]
        if "sts:AssumeRole" not in actions and "sts:*" not in actions:
            continue
        principal = stmt.get("Principal", {})
        aws = _as_list(principal.get("AWS", [])) if isinstance(principal, Mapping) else []
        if caller_arn not in [str(p) for p in aws]:
            continue
        condition = stmt.get("Condition", {})
        equals = condition.get("StringEquals", {}) if isinstance(condition, Mapping) else {}
        required = equals.get("sts:ExternalId") if isinstance(equals, Mapping) else None
        if required is None or (external_id is not None and external_id in [str(v) for v in _as_list(required)]):
            return True
    return False


def moto_trust_policy(role_arn: str) -> Mapping[str, Any] | None:
    """The trust policy of `role_arn` from the in-process moto IAM backend (None if the role does not exist)."""
    from moto.iam.models import iam_backends

    account = role_arn.split(":")[4]
    backend = iam_backends[account]["global"]
    for role in backend.roles.values():
        if role.arn == role_arn:
            doc: Any = role.assume_role_policy_document
            parsed: Mapping[str, Any] = json.loads(doc) if isinstance(doc, str) else doc
            return parsed
    return None


def _request_params(kwargs: Mapping[str, Any]) -> Mapping[str, Any]:
    params = kwargs.get("params", {})
    body = params.get("body") if isinstance(params, Mapping) else None
    return body if isinstance(body, Mapping) else params


def make_handler(caller_arn: str, policy_lookup: PolicyLookup) -> Callable[..., None]:
    """The before-call handler (exposed for unit tests)."""

    def handler(**kwargs: Any) -> None:
        params = _request_params(kwargs)
        role_arn = str(params.get("RoleArn", ""))
        external_id = params.get("ExternalId")
        policy = policy_lookup(role_arn)
        if policy is None or not trust_allows(policy, caller_arn, None if external_id is None else str(external_id)):
            raise ClientError(
                {"Error": {"Code": "AccessDenied", "Message": "trust policy denies the caller"}}, "AssumeRole"
            )

    return handler


def trust_enforcer(client: Any, *, caller_arn: str, policy_lookup: PolicyLookup = moto_trust_policy) -> None:
    """Register the enforcer on an STS client (`client.meta.events`, event `before-call.sts.AssumeRole`)."""
    client.meta.events.register("before-call.sts.AssumeRole", make_handler(caller_arn, policy_lookup))
