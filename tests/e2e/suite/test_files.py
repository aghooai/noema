"""Files through the stack's media worker and sandbox (nsjail): a PDF with active content (sanitized, plus the
`active_content_removed` notice on a turn that uses it), HEIC normalized to PNG, an encrypted PDF and a
decompression bomb rejected, and the derivative download headers (spec-07, spec-09 §6.3, ADR-002)."""

from __future__ import annotations

import io
from typing import Any

import pikepdf
import pytest
from tests.e2e.conftest import CheckedClient, Stack
from tests.e2e.suite.kit import Json, eventually, first, tag
from tests.e2e.suite.turns import invariants
from tests.unit.media_pipeline import media_fixtures as fx

pytestmark = [pytest.mark.e2e]

TERMINAL = frozenset({"ready", "partial", "rejected", "failed"})
INVOICE = "Invoice number 4471"


def javascript_pdf() -> bytes:
    """A one-page PDF with a text layer, an OpenAction JavaScript and a document-level JavaScript name tree."""
    pdf = pikepdf.open(io.BytesIO(fx.text_pdf(INVOICE)))
    script = pikepdf.Dictionary(S=pikepdf.Name.JavaScript, JS=pikepdf.String("app.alert('pwned')"))
    pdf.Root.OpenAction = pdf.make_indirect(script)
    names = pikepdf.Array([pikepdf.String("boot"), pdf.make_indirect(script)])
    pdf.Root.Names = pikepdf.Dictionary(JavaScript=pikepdf.Dictionary(Names=names))
    out = io.BytesIO()
    pdf.save(out)
    return out.getvalue()


async def upload(stack: Stack, name: str, content: bytes, media_type: str, *, replica: int = 0) -> Json:
    """uploadFile on `replica`, then getFile on the other replica until the file is terminal."""
    files = {"file": (name, content, media_type)}
    client = stack.client(replica)
    accepted = (await client.request("POST", "/v1/files", data={"purpose": "input"}, files=files, expect=202)).json()
    assert accepted["status"] == "quarantined" and accepted["bytes"] == len(content)
    path = f"/v1/files/{accepted['id']}"
    return await eventually(stack.client(1 - replica), path, lambda b: b["status"] in TERMINAL, timeout_s=90)


def derivative_status(file: Json) -> dict[str, str]:
    return {d["kind"]: d["status"] for d in file["derivatives"]}


async def download(client: CheckedClient, file_id: str, kind: str, *, page: int | None = None) -> Any:
    params = {"page": page} if page is not None else None
    response = await client.request("GET", f"/v1/files/{file_id}/derivatives/{kind}", params=params, expect=200)
    suffix = f"-{page}" if page is not None else ""
    disposition = response.headers["content-disposition"]
    assert disposition.startswith(f'attachment; filename="{file_id}-{kind}{suffix}.'), disposition
    assert response.headers["x-content-type-options"] == "nosniff"
    return response


@pytest.mark.slow
async def test_pdf_with_active_content_is_sanitized_with_a_notice(stack: Stack) -> None:
    file = await upload(stack, f"invoice-{tag()}.pdf", javascript_pdf(), "application/pdf")
    assert file["status"] == "ready", file
    statuses = derivative_status(file)
    assert statuses["pdf_sanitized"] == statuses["markdown"] == statuses["page_images"] == "ready", statuses
    client = stack.client(0)
    sanitized = await download(client, file["id"], "pdf_sanitized")
    assert sanitized.headers["content-type"].startswith("application/pdf") and sanitized.content.startswith(b"%PDF")
    clean = pikepdf.open(io.BytesIO(sanitized.content))
    assert "/OpenAction" not in clean.Root and "/JavaScript" not in clean.Root.get("/Names", {})
    markdown = await download(client, file["id"], "markdown")
    assert markdown.headers["content-type"].startswith("text/markdown") and INVOICE in markdown.text
    page = await download(client, file["id"], "page_images", page=1)
    assert page.content.startswith(b"\x89PNG") or page.content.startswith(b"\xff\xd8")
    missing_page = await client.request("GET", f"/v1/files/{file['id']}/derivatives/page_images", expect=400)
    assert missing_page.json()["error"]["param"] == "page"

    content = [
        {"type": "input_text", "text": f"What is the invoice number? ({tag()})"},
        {"type": "input_file", "file_id": file["id"]},
    ]
    body: Json = {"input": [{"role": "user", "content": content}]}
    events = await stack.client(1).sse("/v1/turns", json=body)
    invariants(events)
    notices = [
        d for d in first(events, "preflight.completed")["degradations"] if d["reason"] == "active_content_removed"
    ]
    assert notices == [
        {
            "file_id": file["id"],
            "part_index": 1,
            "from": "application/pdf",
            "to": "pdf_sanitized",
            "reason": "active_content_removed",
        }
    ], first(events, "preflight.completed")


async def test_heic_is_normalized_to_png(stack: Stack) -> None:
    file = await upload(stack, f"photo-{tag()}.heic", fx.heic(fx.png(120, 80)), "image/heic", replica=1)
    assert file["status"] in ("ready", "partial") and file["media_type"] == "image/heic", file
    assert derivative_status(file)["raster"] == "ready"
    raster = await download(stack.client(0), file["id"], "raster")
    assert raster.headers["content-type"].startswith("image/png") and raster.content.startswith(b"\x89PNG")
    assert raster.headers["content-disposition"].endswith('-raster.png"')
    thumbnail = await download(stack.client(1), file["id"], "thumbnail")
    assert thumbnail.headers["content-type"].split(";")[0] in ("image/png", "image/jpeg")


@pytest.mark.parametrize(
    ("name", "media_type", "build", "code"),
    [
        ("locked.pdf", "application/pdf", fx.encrypted_pdf, "encrypted_pdf"),
        ("bomb.png", "image/png", lambda: fx.png_ztxt_bomb(64), "decompression_bomb"),
    ],
)
async def test_hostile_files_are_rejected(stack: Stack, name: str, media_type: str, build: Any, code: str) -> None:
    file = await upload(stack, name, build(), media_type)
    assert file["status"] == "rejected" and file["rejection"]["code"] == code, file
    kind = "pdf_sanitized" if media_type == "application/pdf" else "raster"
    refused = await stack.client(0).request("GET", f"/v1/files/{file['id']}/derivatives/{kind}")
    assert refused.status_code in (404, 409), refused.text
    if refused.status_code == 409:
        assert refused.json()["error"]["code"] == "file_rejected"
