"""`parse_query` / `render_report` (spec-11 §2, IMPL-040): defaults, range rules, exact cost serialization."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any

import orjson
import pytest

from noema.core.errors import ErrorCode, NoemaError
from noema.core.protocols.storage import Page
from noema.ops.usage import UsageQuery, parse_query, render_report

pytestmark = pytest.mark.unit

TODAY = date(2026, 10, 10)


def test_defaults_are_the_last_30_days_by_line() -> None:
    assert parse_query({}, TODAY) == UsageQuery("line", date(2026, 9, 11), TODAY, None, 50)
    q = parse_query({"to": "2026-03-31", "group_by": "day", "limit": 7, "after": "abc"}, TODAY)
    assert q == UsageQuery("day", date(2026, 3, 2), date(2026, 3, 31), "abc", 7)
    assert parse_query({"from": "2026-10-01"}, TODAY).day_to == TODAY


def test_366_days_is_the_widest_range() -> None:
    assert parse_query({"from": "2025-10-10", "to": "2026-10-10"}, TODAY).day_from == date(2025, 10, 10)  # 366
    with pytest.raises(NoemaError) as err:
        parse_query({"from": "2025-10-09", "to": "2026-10-10"}, TODAY)  # 367 days
    assert err.value.code is ErrorCode.SCHEMA_VIOLATION and err.value.param == "from"


@pytest.mark.parametrize(
    ("params", "param"),
    [
        ({"from": "2026-10-11", "to": "2026-10-10"}, "from"),
        ({"from": "2026-13-01"}, "from"),
        ({"to": "yesterday"}, "to"),
        ({"to": 20261010}, "to"),
        ({"group_by": "tenant"}, "group_by"),
        ({"limit": 0}, "limit"),
        ({"limit": 201}, "limit"),
        ({"limit": True}, "limit"),
        ({"after": 5}, "after"),
    ],
)
def test_invalid_parameters_are_schema_violations(params: dict[str, Any], param: str) -> None:
    with pytest.raises(NoemaError) as err:
        parse_query(params, TODAY)
    assert err.value.code is ErrorCode.SCHEMA_VIOLATION and err.value.param == param


def row(key: str | None, line: str, cost: object, **counts: int) -> dict[str, object]:
    base = dict.fromkeys(
        ("input_tokens", "output_tokens", "cached_input_tokens", "cache_write_input_tokens", "reasoning_tokens"), 0
    )
    return {**base, **counts, "key": key, "line": line, "cost_usd": cost, "cost_estimated": True,
            "tokens_estimated": False}  # fmt: skip


def test_render_keeps_exact_decimals_and_nulls() -> None:
    q = UsageQuery("session", date(2026, 9, 11), TODAY, None, 50)
    big = Decimal("123456789012.123456")  # numeric(18,6): more digits than a float carries
    page = Page((row('ses_"1"', "decision", big, input_tokens=5), row(None, "waste", Decimal("0E-6"))), True, "cur")
    body = render_report(q, page)
    assert b'"cost_usd":123456789012.123456,' in body and b'"cost_usd":0.000000,' in body
    doc = orjson.loads(body)
    assert doc["group_by"] == "session" and doc["from"] == "2026-09-11" and doc["to"] == "2026-10-10"
    assert doc["has_more"] is True and doc["next_cursor"] == "cur"
    assert [r["key"] for r in doc["data"]] == ['ses_"1"', None]
    assert doc["data"][0]["usage"] == {
        "input_tokens": 5,
        "output_tokens": 0,
        "cached_input_tokens": 0,
        "cache_write_input_tokens": 0,
        "reasoning_tokens": 0,
        "cost_usd": 123456789012.12346,  # the float view; the wire text above is exact
        "cost_estimated": True,
        "tokens_estimated": False,
    }


def test_render_empty_page_and_string_costs() -> None:
    q = UsageQuery("line", TODAY, TODAY, None, 50)
    assert orjson.loads(render_report(q, Page((), False, None)))["data"] == []
    body = render_report(q, Page((row("decision", "decision", "1.5"),), False, None))
    assert b'"cost_usd":1.500000' in body
