"""The /v1/files routes through the real API middleware and the test-mode contract validator (every response must
match openapi): uploadFile (multipart streaming, 202/400/413/415, session authz, Idempotency-Key replay), getFile,
deleteFile (403/404), watchFile (SSE until terminal) and getFileDerivative (spec-07 §6, IMPL-065)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import httpx
import pytest
from tests.fakes.auth import FakeAuthorizer
from tests.unit.api.apifakes import ADMIN_KEY, DEV_KEY, FakeServices, asgi_client, auth, make_app, principal

from noema.api.routes import files as files_routes
from noema.contracts.validate import ContractValidator

from . import media_fixtures as fx
from .support import MediaEnv, md_script, media_env

pytestmark = pytest.mark.unit
OWNER_KEY = "ak_test_01J000000000000000000OWN00." + "o" * 43


@asynccontextmanager
async def client(tmp_path: Path) -> AsyncIterator[tuple[httpx.AsyncClient, MediaEnv]]:
    env = media_env(tmp_path)
    env.runner.script["pdf.markdown"] = md_script(["page one"])
    services = FakeServices()
    services.authn.principals[ADMIN_KEY] = principal(ADMIN_KEY, roles=frozenset({"tenant_admin"}))
    services.authn.principals[OWNER_KEY] = principal(OWNER_KEY)
    extra: dict[str, Any] = {"files": env.files, "authz": FakeAuthorizer(), "db": env.world.db}
    for name, value in extra.items():
        setattr(services, name, value)
    app, _ = make_app(services, routers=[files_routes.router])
    async with asgi_client(app) as http:
        yield http, env


def form(
    data: bytes, *, name: str = "cat.png", ctype: str = "image/png", purpose: str | None = "input", **fields: str
) -> dict[str, Any]:
    payload = {k: v for k, v in {"purpose": purpose, **fields}.items() if v is not None}
    return {"data": payload, "files": {"file": (name, data, ctype)}}


async def upload(http: httpx.AsyncClient, data: bytes, key: str = DEV_KEY, **kwargs: Any) -> httpx.Response:
    headers = auth(key, **kwargs.pop("headers", {}))
    return await http.post("/v1/files", headers=headers, **form(data, **kwargs))


async def test_upload_get_watch_and_download(tmp_path: Path) -> None:
    async with client(tmp_path) as (http, env):
        resp = await upload(http, fx.png(320, 200))
        assert resp.status_code == 202, resp.text
        obj = resp.json()
        assert obj["status"] == "quarantined" and obj["filename"] == "cat.png" and obj["media_type"] == "image/png"
        file_id = obj["id"]
        await env.drain()
        got = (await http.get(f"/v1/files/{file_id}", headers=auth())).json()
        assert got["status"] == "ready" and [d["kind"] for d in got["derivatives"]] == [
            "raster",
            "thumbnail",
            "ocr_text",
        ]
        events = await http.get(f"/v1/files/{file_id}/events", headers=auth())
        assert events.status_code == 200 and events.headers["content-type"].startswith("text/event-stream")
        frames = ContractValidator.parse_sse(events.content)
        assert [(i, e) for i, e, _ in frames] == [("1", "file.updated")]
        thumb = await http.get(f"/v1/files/{file_id}/derivatives/thumbnail", headers=auth())
        assert thumb.status_code == 200 and thumb.content.startswith(b"\x89PNG")
        assert thumb.headers["content-type"] == "image/png"
        assert thumb.headers["content-disposition"] == f'attachment; filename="{file_id}-thumbnail.png"'
        assert thumb.headers["x-content-type-options"] == "nosniff"
        ocr = await http.get(f"/v1/files/{file_id}/derivatives/ocr_text", headers=auth())
        assert ocr.headers["content-type"] == "text/plain; charset=utf-8" and ocr.text == "4471"


async def test_derivative_errors(tmp_path: Path) -> None:
    async with client(tmp_path) as (http, env):
        file_id = (await upload(http, fx.png())).json()["id"]
        pending = await http.get(f"/v1/files/{file_id}/derivatives/thumbnail", headers=auth())
        assert pending.status_code == 409 and pending.json()["error"]["code"] == "file_not_ready"
        await env.drain()
        for path, status, code, param in (
            ("derivatives/raster_1568", 400, "schema_violation", "kind"),
            ("derivatives/page_images", 400, "schema_violation", "page"),
            ("derivatives/thumbnail?page=1", 400, "schema_violation", "page"),
            ("derivatives/markdown", 404, "not_found", None),
            ("derivatives/transcript", 404, "not_found", None),
        ):
            resp = await http.get(f"/v1/files/{file_id}/{path}", headers=auth())
            body = resp.json()["error"]
            assert (resp.status_code, body["code"]) == (status, code), path
            if param:
                assert body["param"] == param, path
        pdf = (await upload(http, fx.text_pdf(), name="a.pdf", ctype="application/pdf")).json()["id"]
        await env.drain()
        page = await http.get(f"/v1/files/{pdf}/derivatives/page_images?page=1", headers=auth())
        assert page.status_code == 200 and page.headers["content-disposition"].endswith(f'"{pdf}-page_images-1.png"')
        beyond = await http.get(f"/v1/files/{pdf}/derivatives/page_images?page=2", headers=auth())
        assert beyond.status_code == 400 and beyond.json()["error"]["param"] == "page"
        bad = (await upload(http, fx.text_pdf(), name="x.png")).json()["id"]
        await env.drain()
        rejected = await http.get(f"/v1/files/{bad}/derivatives/thumbnail", headers=auth())
        assert rejected.status_code == 409 and rejected.json()["error"]["code"] == "file_rejected"


async def test_upload_validation(tmp_path: Path) -> None:
    async with client(tmp_path) as (http, env):
        svg = await upload(http, b"<svg/>", name="a.svg", ctype="image/svg+xml")
        assert svg.status_code == 415 and svg.json()["error"]["code"] == "unsupported_media_type"
        big = await upload(http, b"#" * (5 * 1024 * 1024 + 1), name="a.md", ctype="text/markdown")
        assert big.status_code == 413 and big.json()["error"]["code"] == "file_too_large"
        missing = await upload(http, fx.png(), purpose=None)
        assert missing.status_code == 400 and missing.json()["error"]["param"] == "purpose"
        wrong = await upload(http, fx.png(), purpose="fine-tune")
        assert wrong.status_code == 400 and wrong.json()["error"]["param"] == "purpose"
        no_file = await http.post("/v1/files", headers=auth(), data={"purpose": "input"}, files={"other": ("x", b"x")})
        assert no_file.status_code == 400 and no_file.json()["error"]["param"] == "file"
        unknown_session = await upload(http, fx.png(), session_id="ses_missing")
        assert unknown_session.status_code == 404 and unknown_session.json()["error"]["code"] == "session_not_found"
        assert dict(env.objects()) == {} and env.world.jobs.enqueued == []  # nothing stored by any of these


async def test_session_id_after_the_file_part_is_400(tmp_path: Path) -> None:
    async with client(tmp_path) as (http, env):
        boundary = "b0undary"
        body = (
            (
                f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="a.png"\r\n'
                "Content-Type: image/png\r\n\r\n"
            ).encode()
            + fx.png()
            + (
                f'\r\n--{boundary}\r\nContent-Disposition: form-data; name="session_id"\r\n\r\n'
                f"ses_x\r\n--{boundary}--\r\n"
            ).encode()
        )
        headers = auth(**{"content-type": f"multipart/form-data; boundary={boundary}"})
        resp = await http.post("/v1/files", headers=headers, content=body)
        assert resp.status_code == 400 and resp.json()["error"]["param"] == "session_id"
        assert not [k for k in env.objects() if "/quarantine/" in k]


async def test_unknown_parts_and_blank_lines_are_bounded(tmp_path: Path) -> None:
    """Everything but the file part's data is capped at 64 KiB (413 request_too_large, C1 review), before or after
    the file part, and nothing is stored."""
    async with client(tmp_path) as (http, env):
        big = "x" * (65 * 1024)
        before = await http.post(
            "/v1/files", headers=auth(), data={"purpose": "input", "note": big}, files={"file": ("a.png", fx.png())}
        )
        assert before.status_code == 413 and before.json()["error"]["code"] == "request_too_large"
        boundary = "b0undary"
        head = f'--{boundary}\r\nContent-Disposition: form-data; name="purpose"\r\n\r\ninput\r\n'
        file_part = (
            f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="a.png"\r\n'
            "Content-Type: image/png\r\n\r\n"
        ).encode() + fx.png()
        tail = f'\r\n--{boundary}\r\nContent-Disposition: form-data; name="note"\r\n\r\n{big}\r\n--{boundary}--\r\n'
        headers = auth(**{"content-type": f"multipart/form-data; boundary={boundary}"})
        after = await http.post("/v1/files", headers=headers, content=head.encode() + file_part + tail.encode())
        assert after.status_code == 413 and after.json()["error"]["code"] == "request_too_large"
        flood = await http.post("/v1/files", headers=headers, content=b"\r\n" * 40_000 + head.encode())
        assert flood.status_code == 413
        assert not [k for k in env.objects() if "/quarantine/" in k] and env.world.jobs.enqueued == []
        ok = await upload(http, fx.png(), name="b" * 300 + ".png")  # the extension comes from the full name
        assert ok.status_code == 202 and ok.json()["filename"] == "b" * 251 + ".png"


def seed_session(env: MediaEnv, session_id: str, owner_key: str, project: str = "prj_1") -> None:
    env.world.db.store.seed(
        "session",
        {
            "tenant_id": "t_acme",
            "project_id": project,
            "id": session_id,
            "owner_principal_id": principal(owner_key).principal_id,
            "data_class": "internal",
        },
    )


async def test_delete_visibility_and_permissions(tmp_path: Path) -> None:
    async with client(tmp_path) as (http, env):
        seed_session(env, "ses_owned", OWNER_KEY)
        # a tenant_admin uploads into OWNER's session: visible to the owner (session owner), not theirs to delete
        resp = await upload(http, fx.png(), key=ADMIN_KEY, session_id="ses_owned")
        assert resp.status_code == 202, resp.text
        file_id = resp.json()["id"]
        assert (await http.get(f"/v1/files/{file_id}", headers=auth(OWNER_KEY))).status_code == 200
        forbidden = await http.delete(f"/v1/files/{file_id}", headers=auth(OWNER_KEY))
        assert forbidden.status_code == 403 and forbidden.json()["error"]["code"] == "permission_denied"
        peer = await http.get(f"/v1/files/{file_id}", headers=auth(DEV_KEY))  # same project, neither uploader nor owner
        assert peer.status_code == 404
        deleted = await http.delete(f"/v1/files/{file_id}", headers=auth(ADMIN_KEY))
        assert deleted.status_code == 200 and deleted.json() == {"id": file_id, "deleted": True}
        assert (await http.get(f"/v1/files/{file_id}", headers=auth(ADMIN_KEY))).status_code == 404
        for bad in ("file_unknown", "nope"):
            assert (await http.get(f"/v1/files/{bad}", headers=auth())).status_code == 404


async def test_upload_into_a_session_of_someone_else_is_404_or_403(tmp_path: Path) -> None:
    async with client(tmp_path) as (http, env):
        seed_session(env, "ses_owned", OWNER_KEY)
        seed_session(env, "ses_other_project", OWNER_KEY, project="prj_2")
        hidden = await upload(http, fx.png(), key=DEV_KEY, session_id="ses_owned")
        assert hidden.status_code == 404 and hidden.json()["error"]["code"] == "session_not_found"
        other = await upload(http, fx.png(), key=ADMIN_KEY, session_id="ses_other_project")
        assert other.status_code == 403 and other.json()["error"]["code"] == "permission_denied"
        own = await upload(http, fx.png(), key=OWNER_KEY, session_id="ses_owned")
        assert own.status_code == 202


async def test_idempotent_upload_replays_the_202(tmp_path: Path) -> None:
    async with client(tmp_path) as (http, env):
        headers = {"Idempotency-Key": "upload-1"}
        first = await upload(http, fx.png(), headers=headers)
        second = await upload(http, fx.png(), headers=headers)
        assert first.status_code == second.status_code == 202
        assert second.json()["id"] == first.json()["id"] and second.headers.get("x-aghoo-idempotent-replay") == "true"
        assert len([k for k in env.objects() if "/quarantine/" in k]) == 1
        conflict = await upload(http, fx.png(16, 16), headers=headers)
        assert conflict.status_code == 409 and conflict.json()["error"]["code"] == "idempotency_key_reused"
