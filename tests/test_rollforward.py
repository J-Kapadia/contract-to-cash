"""`c2c rollforward --month M` on `mini`: it closes M, locks every closed month up to M, publishes the
forecast as of M's end in the same transaction, leaves no scratch schema, and a second run changes nothing."""

from __future__ import annotations

import pytest

from c2c import db
from conftest import MINI_BOOKS, MINI_WINDOW, c2c

MONTH, AS_OF = "2026-02", "2026-02-28"
CASES = ["base", "downside", "upside"]
LOCKED = ["2024-10", "2024-11", "2024-12", "2025-01", "2025-02", "2025-03", "2025-04", "2025-05", "2025-06",
          "2025-07", "2025-08", "2025-09", "2025-10", "2025-11", "2025-12", "2026-01", "2026-02"]


def rollforward(warehouse, month: str = MONTH):
    return c2c("rollforward", "--month", month, "--books", str(MINI_BOOKS), "--warehouse", str(warehouse), "--json")


def query(warehouse, sql: str):
    with db.connect(warehouse, read_only=True) as con:
        return con.execute(sql).fetchall()


def months(warehouse) -> list[dict]:
    done = c2c("status", "--warehouse", str(warehouse), "--json")
    assert done.code == 0, done.stderr
    return done.json["months"]


@pytest.fixture(scope="module")
def rolled(tmp_path_factory):
    """A fresh warehouse rolled forward to 2026-02 twice: the first and second envelopes and the status after each."""
    warehouse = tmp_path_factory.mktemp("rollforward") / "w.duckdb"
    first = rollforward(warehouse)
    assert first.code == 0, first.stderr
    after_first = months(warehouse)
    runs = query(warehouse, "SELECT count(*) FROM prov.records WHERE starts_with(number_id, 'fc/')")
    second = rollforward(warehouse)
    assert second.code == 0, second.stderr
    return warehouse, first.json, second.json, after_first, runs


def test_the_months_through_the_rolled_month_are_locked_with_digests(rolled):
    _, envelope, _, after_first, _ = rolled
    assert [m["month"] for m in after_first if m["status"] == "locked"] == LOCKED
    assert all(m["input_digest"] and m["result_hash"] for m in after_first if m["status"] == "locked")
    assert envelope["command"] == "rollforward"
    assert envelope["locked_through"] == MONTH
    assert not [m for m in after_first if m["month"] > MONTH]


def test_the_forecast_is_published_for_the_month_end_one_run_per_case(rolled):
    warehouse, envelope, _, _, _ = rolled
    assert envelope["as_of"] == AS_OF
    assert envelope["cases"] == CASES
    assert query(warehouse, "SELECT CAST(as_of AS VARCHAR), cases FROM forecast.runs") == [(AS_OF, CASES)]
    assert query(warehouse, "SELECT CAST(as_of AS VARCHAR), \"case\" FROM forecast.weekly GROUP BY ALL ORDER BY 2") \
        == [(AS_OF, case) for case in CASES]
    assert [a["case"] for a in envelope["alerts"]] == [
        case for (case,) in query(warehouse, 'SELECT "case" FROM forecast.alerts ORDER BY 1')]


def test_no_scratch_schema_is_left(rolled):
    warehouse, *_ = rolled
    assert query(warehouse, "SELECT count(*) FROM information_schema.schemata WHERE schema_name = 'fc_build'") == [(0,)]


def test_a_second_run_changes_no_hash(rolled):
    warehouse, first, second, after_first, records = rolled
    assert months(warehouse) == after_first
    assert second["result_hash"] == first["result_hash"]
    assert second["forecast_hash"] == first["forecast_hash"]
    assert second["close_status"] == "unchanged"
    assert query(warehouse, "SELECT count(*) FROM forecast.runs") == [(1,)]
    assert query(warehouse, "SELECT count(*) FROM prov.records WHERE starts_with(number_id, 'fc/')") == records


def test_ctl_lock_passes(rolled):
    _, first, second, _, _ = rolled
    for envelope in (first, second):
        lock = [c for c in envelope["controls"] if c["id"] == "CTL-LOCK"]
        assert lock and all(c["status"] == "pass" for c in lock)


def test_rolling_forward_closes_an_unclosed_month_first(tmp_path):
    warehouse = tmp_path / "w.duckdb"
    closed = c2c("close", "--month", "2026-01", "--books", str(MINI_BOOKS), "--warehouse", str(warehouse), "--json")
    assert closed.code == 0, closed.stderr
    assert query(warehouse, f"SELECT count(*) FROM state.close_state WHERE month = '{MONTH}'") == [(0,)]
    done = rollforward(warehouse)
    assert done.code == 0, done.stderr
    statuses = dict(query(warehouse, "SELECT month, status FROM state.close_state ORDER BY month"))
    assert [m for m, s in statuses.items() if s == "locked"] == LOCKED
    assert MINI_WINDOW[1] not in statuses


def test_a_month_outside_the_books_exits_2_and_publishes_nothing(tmp_path):
    warehouse = tmp_path / "w.duckdb"
    done = rollforward(warehouse, "2030-01")
    assert done.code == 2
    assert not warehouse.exists() or query(
        warehouse, "SELECT count(*) FROM information_schema.schemata WHERE schema_name = 'forecast'") == [(0,)]
