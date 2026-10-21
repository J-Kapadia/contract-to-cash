"""`c2c backtest` on `mini` closed through its last month: which as-of months are scored and which are skipped
with a reason, that the metrics equal a recomputation from `forecast.daily` and the books, that a month before
eligibility is reported rather than scored, the closed-months precondition, and that no answer key is read."""

from __future__ import annotations

import csv
import json
import shutil
from datetime import date, timedelta

import pytest

from c2c import db
from conftest import MINI_BOOKS, ROOT, c2c
from forecast_helpers import mini_closed, mini_closed_copy  # noqa: F401  (fixtures)

CASES = ("base", "downside", "upside")
SCORED = ("2025-10", "2025-11")
BEFORE = [f"2024-{m:02d}" for m in range(10, 13)] + [f"2025-{m:02d}" for m in range(1, 10)]
BEYOND = ["2025-12", "2026-01", "2026-02", "2026-03"]
METRICS = ("cash_in_wape_bps", "net_cash_error_cents", "ending_balance_error_cents", "baseline_wape_bps",
           "revenue_3m_error_bps")


def backtest(warehouse, *args: str, books=MINI_BOOKS):
    # No answer key: the truth directory named here does not exist.
    return c2c("backtest", "--books", str(books), "--truth", str(warehouse.parent / "no-truth"),
               "--warehouse", str(warehouse), "--json", *args)


def query(warehouse, sql: str, params=()):
    with db.connect(warehouse, read_only=True) as con:
        return con.execute(sql, params).fetchall()


@pytest.fixture(scope="module")
def scored(mini_closed, tmp_path_factory):  # noqa: F811
    warehouse = tmp_path_factory.mktemp("backtest") / "w.duckdb"
    shutil.copyfile(mini_closed, warehouse)
    done = backtest(warehouse)
    assert done.code == 0, done.stderr
    return warehouse, json.loads(done.stdout)


def test_exactly_the_eligible_months_are_scored_and_the_others_skipped_with_a_reason(scored):
    _, envelope = scored
    assert envelope["command"] == "backtest"
    assert envelope["exit"] == 0
    assert sorted({r["as_of"] for r in envelope["rows"]}) == ["2025-10-31", "2025-11-30"]
    assert [(r["as_of"], r["case"]) for r in envelope["rows"]] == [(f"{m}-{d}", c) for m, d in
                                                                  (("2025-10", 31), ("2025-11", 30)) for c in CASES]
    assert envelope["skipped"] == ([{"month": m, "reason": "insufficient_history"} for m in BEFORE]
                                   + [{"month": m, "reason": "horizon_beyond_books"} for m in BEYOND])


def test_the_rows_are_written_to_the_backtest_table(scored):
    warehouse, envelope = scored
    rows = query(warehouse, f"SELECT CAST(as_of AS VARCHAR), \"case\", {', '.join(METRICS)} "
                            "FROM forecast.backtest ORDER BY as_of, \"case\"")
    assert rows == [(r["as_of"], r["case"], *(r[m] for m in METRICS)) for r in envelope["rows"]]


def test_each_as_of_is_published_as_a_forecast_of_its_own(scored):
    warehouse, _ = scored
    assert query(warehouse, "SELECT CAST(as_of AS VARCHAR) FROM forecast.runs ORDER BY as_of") == [
        ("2025-10-31",), ("2025-11-30",)]


def _bank():
    with open(MINI_BOOKS / "bank_transactions.csv", newline="") as handle:
        rows = list(csv.DictReader(handle))
    opening = min(rows, key=lambda r: (r["posted_on"], r["bank_txn_id"]))
    return [(date.fromisoformat(r["posted_on"]), date.fromisoformat(r["known_on"]), int(r["amount_cents"]),
             r is opening) for r in rows]


def _wape(forecasts, actuals):
    return sum(abs(f - a) for f, a in zip(forecasts, actuals)) * 10_000 // sum(actuals)


@pytest.mark.parametrize("as_of", [date(2025, 10, 31), date(2025, 11, 30)])
@pytest.mark.parametrize("case", CASES)
def test_the_metrics_equal_a_recomputation_from_the_forecast_and_the_books(scored, as_of, case):
    warehouse, envelope = scored
    (row,) = [r for r in envelope["rows"] if r["as_of"] == as_of.isoformat() and r["case"] == case]
    horizon = [as_of + timedelta(days=d) for d in range(1, 92)]
    bank = _bank()

    def week(day):
        return (day - as_of).days // 7 - (1 if (day - as_of).days % 7 == 0 else 0)

    daily = query(warehouse, 'SELECT date, direction, cents FROM forecast.daily WHERE as_of = ? AND "case" = ?',
                  [as_of, case])
    forecast_in = [0] * 13
    for day, direction, cents in daily:
        if direction == "in" and day in horizon:
            forecast_in[week(day)] += cents
    actual_in = [0] * 13
    for posted, _, amount, opening in bank:
        if posted in horizon and amount > 0 and not opening:
            actual_in[week(posted)] += amount
    trailing = sum(a for p, k, a, o in bank if not o and a > 0 and as_of - timedelta(days=91) < p <= as_of
                   and k <= as_of)
    forecast_net = sum(c if d == "in" else -c for day, d, c in daily if day in horizon)
    actual_net = sum(a for p, _, a, o in bank if p in horizon and not o)
    balance = sum(a for p, _, a, _ in bank if p <= horizon[-1])
    (ending,) = {e for (e,) in query(warehouse, "SELECT ending_cents FROM forecast.weekly WHERE as_of = ? AND "
                                                "\"case\" = ? AND week = 'W13'", [as_of, case])}
    months = {f"{as_of.year + (as_of.month + k - 1) // 12:04d}-{(as_of.month + k - 1) % 12 + 1:02d}" for k in (1, 2, 3)}
    (revenue,) = query(warehouse, 'SELECT sum(recognised_cents) FROM forecast.revenue WHERE as_of = ? AND "case" = ? '
                                  "AND month IN (SELECT unnest(?))", [as_of, case, sorted(months)])[0]
    (booked,) = query(warehouse, "SELECT sum(recognised_cents) FROM mart.fct_revenue WHERE month IN "
                                 "(SELECT unnest(?))", [sorted(months)])[0]

    assert sum(actual_in) > 0 and trailing > 0
    assert row["cash_in_wape_bps"] == _wape(forecast_in, actual_in)
    assert row["baseline_wape_bps"] == _wape([trailing // 13] * 13, actual_in)
    assert row["net_cash_error_cents"] == forecast_net - actual_net
    assert row["ending_balance_error_cents"] == ending - balance
    assert row["revenue_3m_error_bps"] == abs(revenue - booked) * 10_000 // booked


def test_a_month_before_eligibility_is_reported_with_its_reason_and_not_scored(mini_closed_copy):
    done = backtest(mini_closed_copy, "--from", "2025-09", "--to", "2025-09")
    assert done.code == 0, done.stderr
    envelope = json.loads(done.stdout)
    assert envelope["rows"] == []
    assert envelope["skipped"] == [{"month": "2025-09", "reason": "insufficient_history"}]
    assert query(mini_closed_copy, "SELECT count(*) FROM information_schema.tables "
                                   "WHERE table_schema = 'forecast'") == [(0,)]


def test_a_range_that_includes_both_kinds_of_skip_scores_what_is_eligible(mini_closed_copy):
    done = backtest(mini_closed_copy, "--from", "2025-10", "--to", "2025-12")
    assert done.code == 0, done.stderr
    envelope = json.loads(done.stdout)
    assert {r["as_of"] for r in envelope["rows"]} == {"2025-10-31", "2025-11-30"}
    assert envelope["skipped"] == [{"month": "2025-12", "reason": "horizon_beyond_books"}]


def test_a_month_that_is_not_closed_is_refused_with_exit_4(mini_closed_copy):
    with db.connect(mini_closed_copy) as con:
        con.execute("UPDATE state.close_state SET status = 'open' WHERE month = '2025-05'")
    done = backtest(mini_closed_copy)
    assert done.code == 4
    assert json.loads(done.stdout)["message"].startswith("month 2025-05 not closed")


def test_a_month_outside_the_books_is_a_usage_error(mini_closed_copy):
    assert backtest(mini_closed_copy, "--from", "2030-01").code == 2
    assert backtest(mini_closed_copy, "--from", "2025-11", "--to", "2025-10").code == 2


def test_the_backtest_reads_nothing_under_truth():
    # `scored` ran with the truth directory named and absent; the sources never name it either.
    assert not (ROOT / "tests" / "fixtures" / "books" / "truth").exists()
    for name in ("backtest.py", "commands/backtest.py"):
        assert "truth" not in (ROOT / "src" / "c2c" / name).read_text().lower().replace("--truth", "")
