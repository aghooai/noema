"""`noema.derivative` fact columns: `text_layer_pages` (spec-07 §4.5, ADR-002 §5 pdf.render facts; page_images
only, 1-based) and `facts` (the pdf.sanitize active content, spec-09 §6.3 step 5; pdf_sanitized only)."""

from __future__ import annotations

import asyncpg
import pytest
from tests.fixtures.db import new_tenant_id
from tests.integration.db._rows import Conn, seed_parents

pytestmark = pytest.mark.integration
_INSERT = (
    "INSERT INTO noema.derivative (tenant_id, id, file_id, kind, status, extractor_version, text_layer_pages)"
    " VALUES ($1, $2, 'file_prj_a', $3, 'ready', 'x1', $4::int[])"
)


async def test_text_layer_pages_belong_to_page_images(pg_admin: Conn) -> None:
    tenant = new_tenant_id()
    await seed_parents(pg_admin, tenant)
    await pg_admin.execute(_INSERT, tenant, "der_pi", "page_images", [1, 3])
    await pg_admin.execute(_INSERT, tenant, "der_raster", "raster", None)
    stored = await pg_admin.fetchval("SELECT text_layer_pages FROM noema.derivative WHERE id = 'der_pi'")
    assert stored == [1, 3]
    for row_id, kind, pages in (("der_bad_kind", "pdf_sanitized", [1]), ("der_zero", "page_images", [0, 2])):
        with pytest.raises(asyncpg.CheckViolationError):
            await pg_admin.execute(_INSERT, tenant, row_id, kind, pages)


_FACTS = (
    "INSERT INTO noema.derivative (tenant_id, id, file_id, kind, status, extractor_version, facts)"
    " VALUES ($1, $2, 'file_prj_a', $3, 'ready', 'x1', $4::jsonb)"
)


async def test_facts_are_closed_per_kind(pg_admin: Conn) -> None:
    """media-pipeline request 2: pdf_sanitized {"active_content": [spec-09 §6.3 values]} only; NULL elsewhere."""
    tenant = new_tenant_id()
    await seed_parents(pg_admin, tenant)
    await pg_admin.execute(_FACTS, tenant, "der_ps", "pdf_sanitized", {"active_content": ["javascript", "xfa"]})
    await pg_admin.execute(_FACTS, tenant, "der_raster", "raster", None)
    stored = await pg_admin.fetchval(
        "SELECT facts FROM noema.derivative WHERE tenant_id = $1 AND id = 'der_ps'", tenant
    )
    assert stored == {"active_content": ["javascript", "xfa"]}
    await pg_admin.execute("DELETE FROM noema.derivative WHERE tenant_id = $1 AND id = 'der_ps'", tenant)
    await pg_admin.execute(_FACTS, tenant, "der_ps_none", "pdf_sanitized", {"active_content": []})
    for row_id, kind, facts in (
        ("der_kind", "raster", {"active_content": []}),  # another kind
        ("der_key", "pdf_sanitized", {"active_content": [], "pages": 3}),  # an unknown key
        ("der_missing", "pdf_sanitized", {}),
        ("der_scalar", "pdf_sanitized", "javascript"),
        ("der_list", "pdf_sanitized", ["javascript"]),
        ("der_type", "pdf_sanitized", {"active_content": "javascript"}),
        ("der_value", "pdf_sanitized", {"active_content": ["javascript", "macro"]}),  # outside the spec-09 table
        ("der_number", "pdf_sanitized", {"active_content": [1]}),
    ):
        await pg_admin.execute("DELETE FROM noema.derivative WHERE tenant_id = $1 AND kind = $2", tenant, kind)
        with pytest.raises(asyncpg.CheckViolationError):
            await pg_admin.execute(_FACTS, tenant, row_id, kind, facts)
