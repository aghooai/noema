"""Credential flow of both adapters (ADR-001 §2, §4, §6; IMPL-076): `broker.get` + `assert_creds_for` before any
byte (SEC-STS-08 adapter side), fail-closed broker causes, signing only with the broker's credentials even with
ambient `AWS_*` set (SEC-STS-15 adapter side), per-tenant client pools, `ExpiredTokenException` invalidation, the
broker-backed refreshable credentials' windows (SEC-STS-17 adapter side) and no secret in the adapters' own logs
(SEC-STS-12/13; botocore's DEBUG records are redacted by the process log filter, spec-09 §4)."""

from __future__ import annotations

import logging
import re
from datetime import timedelta
from typing import Any

import pytest
from tests.fakes.clock import FakeClock, FakeIdGen
from tests.fakes.outbox import FakeOutbox
from tests.fakes.telemetry import FakeTelemetry
from tests.unit.adapters.support import SOL, Rig, Unary, collect, json_reply, request, rig

from noema.adapters._base import SecurityEventSink
from noema.adapters._unary import RawResponse
from noema.core.errors import ErrorKind
from noema.core.protocols.adapters import AdapterError, TextDelta
from noema.identity.security import SecurityEvents


async def call(r: Rig, alias: str, *, stream: bool) -> None:
    if stream:
        await collect(r, alias)
    else:
        async with r.clock.autojump():
            await r.registry.for_model(alias).complete(request(alias), tenant=r.tenant, deadline=r.deadline())


@pytest.mark.parametrize("alias", ["sol-6.1", "deepseek-3.2"])
@pytest.mark.parametrize("stream", [False, True])
async def test_swapped_credentials_fail_before_any_wire_byte(alias: str, stream: bool) -> None:
    async with rig(two_tenants=True) as r:
        r.broker.swap("t_acme", "t_beta")
        with pytest.raises(AdapterError) as err:
            await call(r, alias, stream=stream)
        assert r.emulator.requests == [] and r.emulator.wire == []
        assert r.metric("aghoo_security_violations_total", kind="account_mismatch") == 1
    assert err.value.error.kind is ErrorKind.TENANT_ACCESS and err.value.error.cause == "account_mismatch"


async def test_account_mismatch_is_a_structured_security_record(caplog: pytest.LogCaptureFixture) -> None:
    # adapters review (spec-09 §1): {"event": "security", name, tenant_id, request_id, kind} + the counter
    caplog.set_level(logging.WARNING, logger="noema.adapters.security")
    async with rig(two_tenants=True) as r:
        r.broker.swap("t_acme", "t_beta")
        with pytest.raises(AdapterError):
            await call(r, "sol-6.1", stream=False)
        assert r.metric("aghoo_tenant_access_failures_total", tenant="t_acme", cause="account_mismatch") == 1
    (record,) = [rec for rec in caplog.records if rec.name == "noema.adapters.security"]
    assert (record.event, record.code, record.tenant) == ("security", "account_mismatch", "t_acme")  # type: ignore[attr-defined]
    assert record.getMessage() == "security name=account_mismatch tenant_id=t_acme request_id= kind=account_mismatch"


@pytest.mark.parametrize("alias", ["sol-6.1", "deepseek-3.2"])
async def test_account_mismatch_goes_through_the_security_sink_with_its_audit_row(alias: str) -> None:
    clock, telemetry, outbox = FakeClock(), FakeTelemetry(), FakeOutbox()
    sink: SecurityEventSink = SecurityEvents(telemetry=telemetry, outbox=outbox, ids=FakeIdGen(clock), clock=clock)
    async with rig(clock=clock, two_tenants=True, security=sink) as r:
        r.broker.swap("t_acme", "t_beta")
        with pytest.raises(AdapterError):
            await call(r, alias, stream=True)
        assert r.metric("aghoo_security_violations_total") == 0  # counted once, by the sink
    (audit,) = outbox.of_kind("audit_event")
    assert audit.tenant_id == "t_acme" and audit.payload["action"] == "security.account_mismatch"
    assert audit.payload["details"] == {"source": "adapter"}
    assert [m.labels for m in telemetry.metrics if m.name == "aghoo_security_violations_total"] == [
        {"kind": "account_mismatch"}
    ]


@pytest.mark.parametrize("cause", ["access_denied", "external_id_invalid", "sts_unavailable"])
async def test_broker_failures_fail_closed_with_their_cause(cause: Any) -> None:
    async with rig() as r:
        r.broker.fail("t_acme", cause)
        with pytest.raises(AdapterError) as err:
            await call(r, "sol-6.1", stream=False)
        assert r.emulator.requests == []
    assert err.value.error.kind is ErrorKind.TENANT_ACCESS and err.value.error.cause == cause


async def test_ambient_aws_variables_never_sign_tenant_calls(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "AKIAAMBIENTAMBIENT00")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "ambient-secret")
    monkeypatch.setenv("AWS_PROFILE", "third-account")
    monkeypatch.setenv("AWS_BEARER_TOKEN_BEDROCK", "bedrock-api-key-ambient")  # botocore reads it from os.environ
    async with rig(mantle_mode="sigv4") as r:
        await call(r, "sol-6.1", stream=False)
        await call(r, "deepseek-3.2", stream=True)
        creds = await r.broker.get("t_acme")
    assert [q.akid for q in r.emulator.requests] == [creds.aws.access_key_id] * 2
    assert all(q.auth == "sigv4" for q in r.emulator.requests)


async def test_clients_are_pooled_per_tenant_and_region() -> None:
    async with rig(two_tenants=True) as r:
        beta = r.config.current().tenant_ref("t_beta")
        for _ in range(3):
            await call(r, "sol-6.1", stream=False)
        async with r.clock.autojump():
            await r.converse.complete(request("sol-6.1"), tenant=beta, deadline=r.deadline())
        clients = r.emulator.unary_clients
    assert [(c.creds.tenant_id, c.creds.region) for c in clients] == [("t_acme", "us-east-1"), ("t_beta", "us-west-2")]


def expired_once() -> Unary:
    def respond(n: int, *_: Any) -> RawResponse:
        if n == 1:
            body = {"message": "The security token included in the request is expired"}
            return json_reply(body, 403, {"x-amzn-errortype": "ExpiredTokenException"})
        return json_reply(
            {
                "output": {"message": {"role": "assistant", "content": [{"text": "ok"}]}},
                "stopReason": "end_turn",
                "usage": {"inputTokens": 1, "outputTokens": 1, "totalTokens": 2},
            }
        )

    return Unary(respond)


async def test_expired_token_invalidates_the_broker_and_retires_the_client() -> None:
    unary = expired_once()
    async with rig(converse_unary=unary) as r:
        with pytest.raises(AdapterError) as err:
            await call(r, "sol-6.1", stream=False)
        assert r.broker.invalidations["t_acme"] == 1
        await call(r, "sol-6.1", stream=False)  # a fresh client, signed with re-fetched credentials
        assert r.metric("aghoo_tenant_access_failures_total", tenant="t_acme", cause="access_denied") == 1
    assert err.value.error.kind is ErrorKind.TENANT_ACCESS and err.value.error.cause == "access_denied"
    assert len(unary.clients) == 2
    first, second = (re.search(r"Credential=(\w+)/", c[1]["Authorization"]) for c in unary.calls)
    assert first is not None and second is not None and first.group(1) != second.group(1)


async def test_every_call_signs_with_the_brokers_current_credentials() -> None:
    # SEC-STS-17 adapter side (E sprint A): no adapter-held refreshable credentials any more; each call signs with
    # its own broker.get, so the broker's windows apply: the same session before the mandatory refresh, then the
    # re-minted one (the advisory refresh does not mint)
    async with rig() as r:
        first = await r.broker.get("t_acme")
        assert first.aws.expires_at is not None
        exp = first.aws.expires_at

        async def at(before_exp: timedelta, *, stream: bool) -> None:
            await r.clock.advance_to(r.clock.monotonic() + (exp - before_exp - r.clock.now()).total_seconds())
            await call(r, "sol-6.1", stream=stream)

        await at(timedelta(minutes=16), stream=False)
        await at(timedelta(minutes=9), stream=True)  # advisory: the broker keeps its session
        assert r.broker.mints["t_acme"] == 1
        await at(timedelta(minutes=4), stream=False)  # mandatory: the broker re-mints
        assert r.broker.mints["t_acme"] == 2
        renewed = await r.broker.get("t_acme")
    akids = [q.akid for q in r.emulator.requests]
    assert akids == [first.aws.access_key_id] * 2 + [renewed.aws.access_key_id]
    assert renewed.aws.access_key_id != first.aws.access_key_id


async def test_no_secret_reaches_the_logs(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    async with rig() as r:
        r.scenario(
            {"match": {"model": SOL}, "fault": {"kind": "throttling"}}, {"match": {}, "stream": {"chunks": ["x"]}}
        )
        with pytest.raises(AdapterError):
            await call(r, "sol-6.1", stream=False)
        events = [e for _, e in await collect(r, "deepseek-3.2")]
        creds = await r.broker.get("t_acme")
    assert any(isinstance(e, TextDelta) for e in events)
    assert creds.mantle.bearer_token is not None and creds.aws.session_token is not None
    secrets = [creds.aws.secret_access_key, creds.aws.session_token, creds.mantle.bearer_token]
    ours = [rec for rec in caplog.records if rec.name.startswith(("noema", "httpx"))]
    assert any(rec.name == "noema.adapters" for rec in ours)  # the throttle was logged
    logged = " ".join(f"{rec.getMessage()} {rec.__dict__}" for rec in ours)
    assert not any(s.get_secret_value() in logged for s in secrets)
