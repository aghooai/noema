"""The statement builders and keyset cursors of noema.db.repos._base (pure; no database)."""

from __future__ import annotations

import pytest

from noema.core.errors import ErrorCode, NoemaError
from noema.db.repos import _tables as t
from noema.db.repos import decode_cursor, encode_cursor
from noema.db.repos._base import Query, affected, check_limit, eq_filters, insert_sql, set_clause
from noema.db.uow import Scope, Tx

pytestmark = pytest.mark.unit


def _tx(project: str = "prj_a") -> Tx:
    return Tx(conn=None, scope=Scope("t_a", project))  # type: ignore[arg-type]  # builders never touch conn


def test_insert_fills_the_scope_and_binds_every_value() -> None:
    sql, args = insert_sql(t.SESSION, _tx(), {"id": "ses_1", "metadata": {"a": 1}})
    assert sql == (
        'INSERT INTO noema.session ("id", "metadata", "tenant_id", "project_id") VALUES ($1, $2, $3, $4) RETURNING *'
    )
    assert args == ["ses_1", {"a": 1}, "t_a", "prj_a"]
    sql, args = insert_sql(t.SESSION, _tx(""), {"id": "ses_1", "project_id": "prj_b"}, suffix="")
    assert args == ["ses_1", "prj_b", "t_a"]  # tenant-wide uow: project from the row


def test_insert_uses_sql_defaults_and_rejects_unknown_columns() -> None:
    sql, _ = insert_sql(t.CALIBRATOR, _tx(), {"id": "cal_1"}, suffix="")
    assert sql.endswith('"fitted_at") VALUES ($1, $2, now()) ')
    with pytest.raises(ValueError, match=r"\['x; DROP TABLE'\]"):
        insert_sql(t.SESSION, _tx(), {"x; DROP TABLE": 1})
    with pytest.raises(ValueError, match="day"):
        insert_sql(t.TURN, _tx(), {"id": "turn_1", "day": "2026-01-01"})


def test_set_clause_and_filters() -> None:
    q = Query("t_a", "ses_1")
    assert set_clause(t.SESSION, q, {"metadata": {}, "expires_at": None}, frozenset({"metadata", "expires_at"})) == (
        '"metadata" = $3, "expires_at" = $4'
    )
    with pytest.raises(ValueError, match="last_seq"):
        set_clause(t.SESSION, q, {"last_seq": 1}, frozenset({"metadata"}))
    assert eq_filters(q, status="ready", kind=None) == ["status = $5"]
    assert q.args == ["t_a", "ses_1", {}, None, "ready"]


@pytest.mark.parametrize("key", [["ses_01"], [3], [None, "line"], ["k", "waste"]])
def test_cursor_round_trip(key: list[str | int | None]) -> None:
    cursor = encode_cursor(key)
    assert "=" not in cursor
    shape = [(str, int, type(None))] * len(key)
    assert decode_cursor(cursor, shape) == key


@pytest.mark.parametrize(
    "cursor", ["", "!!", "e30", "WzFd", "WyJhIiwiYiJd", "W3RydWVd", "ñ"]
)  # {}, [1], 2 items, [true]
def test_malformed_cursors_are_schema_violations(cursor: str) -> None:
    with pytest.raises(NoemaError) as err:
        decode_cursor(cursor, [(str,)])
    assert (err.value.code, err.value.param) == (ErrorCode.SCHEMA_VIOLATION, "after")


def test_limits_and_command_counts() -> None:
    assert check_limit(1) == 1 and check_limit(200) == 200
    for bad in (0, 201, -1, True):
        with pytest.raises(NoemaError) as err:
            check_limit(bad)
        assert err.value.param == "limit"
    assert (affected("DELETE 3"), affected("UPDATE 0"), affected("SELECT")) == (3, 0, 0)
