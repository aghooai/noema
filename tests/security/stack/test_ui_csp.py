"""SE11, the CSP half (06 §9 "Output handling"; spec-09 §8) on the dev cookie proxy in front of the security
stack (`NOEMA_SEC_UI_URL`, started by `make qa-security-ui`): every kind of response (the SPA document, a built
script and stylesheet, an unknown path, `/auth/session`, a proxied API read, a proxied API error, a CSRF refusal)
carries exactly the spec-09 §8 CSP directives, `Referrer-Policy: no-referrer` and `X-Content-Type-Options: nosniff`;
the document has no inline script or style and references nothing outside 'self'. The proxied (and the proxy's own
`/v1`) responses are contract-validated. The browser half (0
`securitypolicyviolation` events across the flows, the Markdown corpus inert: SEC-UI-01/02) is the ui module's
Playwright suite, run by the same make target."""

from __future__ import annotations

import os
import re
from collections.abc import AsyncIterator
from typing import Final

import httpx
import pytest

from noema.contracts.validate import ContractValidator

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.skipif(not os.environ.get("NOEMA_SEC_UI_URL"), reason="needs the dev proxy: make qa-security-ui"),
]
CSP: Final = {
    "default-src": ["'self'"],
    "img-src": ["'self'", "blob:"],
    "script-src": ["'self'"],
    "style-src": ["'self'"],
    "connect-src": ["'self'"],
    "object-src": ["'none'"],
    "frame-ancestors": ["'none'"],
    "base-uri": ["'none'"],
    "form-action": ["'self'"],
}


def directives(header: str) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for part in header.split(";"):
        if part.strip():
            name, *values = part.split()
            assert name not in out, f"duplicate directive {name}"
            out[name] = values
    return out


@pytest.fixture
async def ui() -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(base_url=os.environ["NOEMA_SEC_UI_URL"], timeout=30) as http:
        yield http


async def test_every_response_carries_the_spec_headers(ui: httpx.AsyncClient) -> None:
    origin = str(ui.base_url).rstrip("/")
    doc = await ui.get("/")
    assets = re.findall(r'(?:src|href)="(/[^"]+\.(?:js|css))"', doc.text)
    assert doc.status_code == 200 and assets, doc.text[:300]
    session = await ui.get("/auth/session")
    csrf = session.cookies.get("noema_csrf") or ui.cookies.get("noema_csrf") or ""
    login = await ui.post("/auth/login", json={"alias": "developer"},
                          headers={"x-csrf-token": csrf, "origin": origin})  # fmt: skip
    assert login.status_code in (200, 204), login.text
    csrf = ui.cookies.get("noema_csrf") or csrf
    responses = {
        "document": doc,
        **{f"asset {a}": await ui.get(a) for a in assets},
        "unknown path": await ui.get("/no/such/file.js"),
        "auth session": session,
        "auth login": login,
        "api read": await ui.get("/v1/profiles", headers={"aghoo-version": "2026-10-01"}),
        "api error": await ui.get("/v1/sessions/ses_00000000000000000000000000"),
        "csrf refusal": await ui.post("/v1/sessions", json={}, headers={"origin": origin}),
        "proxied mutation": await ui.post("/v1/sessions", json={}, headers={"origin": origin, "x-csrf-token": csrf}),
    }
    assert responses["api read"].status_code == 200 and responses["csrf refusal"].status_code == 403
    validator = ContractValidator()
    for name, op, body in (
        ("api read", "listProfiles", None),
        ("api error", "getSession", None),
        ("csrf refusal", "createSession", {}),
        ("proxied mutation", "createSession", {}),
    ):
        r = responses[name]
        validator.check_response(op, r.status_code, dict(r.headers), r.content, body)  # fmt: skip
    assert responses["proxied mutation"].status_code == 201, responses["proxied mutation"].text
    problems = []
    for name, response in responses.items():
        headers = response.headers
        if directives(headers.get("content-security-policy", "")) != CSP:
            problems.append(f"{name}: CSP {headers.get('content-security-policy')!r}")
        if headers.get("referrer-policy") != "no-referrer" or headers.get("x-content-type-options") != "nosniff":
            problems.append(f"{name}: {headers.get('referrer-policy')} {headers.get('x-content-type-options')}")
        if "set-cookie" in headers and name.startswith("api"):
            problems.append(f"{name}: an upstream set-cookie was forwarded")
    assert problems == [], problems


async def test_the_document_has_no_inline_or_remote_code(ui: httpx.AsyncClient) -> None:
    html = (await ui.get("/")).text
    inline_scripts = [m for m in re.findall(r"<script\b([^>]*)>", html, re.I) if "src=" not in m]
    assert inline_scripts == [], inline_scripts
    assert not re.search(r"<style\b|\sstyle=|\son[a-z]+=", html, re.I), html[:500]
    remote = re.findall(r'(?:src|href)="((?:https?:)?//[^"]+)"', html, re.I)
    assert remote == [], remote
