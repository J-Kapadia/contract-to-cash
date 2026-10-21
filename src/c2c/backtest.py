"""The backtest: the forecast re-run as of earlier month-ends and scored against what the books later show.

An as-of month-end A is scored when the books hold enough history before it and cover its whole 13-week
horizon. Each scored as-of goes through `forecast.run`, the code path of the live forecast, and is then
scored from what that run published to `forecast.daily`, `forecast.weekly` and `forecast.revenue`
against the books' bank rows and recognised revenue. Nothing here reads the answer key.
"""

from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

import duckdb

from . import db, forecast, state
from .errors import PreconditionError
from .policy import CASE_NAMES, Policy

WEEKS = 13
HORIZON_DAYS = 7 * WEEKS
REVENUE_MONTHS = 3
BPS = 10_000
INSUFFICIENT_HISTORY, HORIZON_BEYOND_BOOKS = "insufficient_history", "horizon_beyond_books"
METRICS = ("cash_in_wape_bps", "net_cash_error_cents", "ending_balance_error_cents", "baseline_wape_bps",
           "revenue_3m_error_bps")
TABLE = ('as_of DATE, "case" VARCHAR, cash_in_wape_bps BIGINT, net_cash_error_cents BIGINT, '
         "ending_balance_error_cents BIGINT, baseline_wape_bps BIGINT, revenue_3m_error_bps BIGINT")

# The bank statement as the scorer reads it; its first row (earliest posting, then lowest id) is the
# opening balance, which is a balance and not a receipt.
BANK = ("SELECT posted_on::date AS posted_on, known_on::date AS known_on, amount_cents::bigint AS amount_cents, "
        "row_number() OVER (ORDER BY posted_on::date, bank_txn_id) = 1 AS is_opening FROM raw.bank_transactions")


def split(books: tuple[str, str], months: list[str], policy: Policy) -> tuple[list[str], list[dict]]:
    """`(eligible, skipped)` among `months` of the books' window `books`.

    A month is eligible when at least `backtest_min_history_months` of the books' months precede it and
    its end plus 91 days is on or before the last day of the books; `skipped` gives the reason for each
    of the others, `insufficient_history` first."""
    first, last = books
    ordered = list(state.months(first, last))
    books_end = forecast.month_end(last)
    eligible, skipped = [], []
    for month in months:
        if ordered.index(month) < policy.backtest_min_history_months:
            skipped.append({"month": month, "reason": INSUFFICIENT_HISTORY})
        elif forecast.month_end(month) + timedelta(days=HORIZON_DAYS) > books_end:
            skipped.append({"month": month, "reason": HORIZON_BEYOND_BOOKS})
        else:
            eligible.append(month)
    return eligible, skipped


def wape_bps(forecasts: list[int], actuals: list[int]) -> int | None:
    """Σ|forecast − actual| ÷ Σ|actual| in basis points, rounded down; None when nothing was received."""
    total = sum(abs(a) for a in actuals)
    if total == 0:
        return None
    return sum(abs(f - a) for f, a in zip(forecasts, actuals)) * BPS // total


def _weekly(rows: list[tuple[int, int]]) -> list[int]:
    cents = [0] * WEEKS
    for week, amount in rows:
        cents[week] = amount
    return cents


def actual_receipts(con: duckdb.DuckDBPyConnection, as_of: date) -> list[int]:
    """The books' receipts in each of the 13 weeks after `as_of`."""
    return _weekly(con.execute(
        f"SELECT (posted_on - ?::date - 1) // 7, sum(amount_cents) FROM ({BANK}) "
        "WHERE NOT is_opening AND amount_cents > 0 AND posted_on > ?::date AND posted_on <= ?::date + ? "
        "GROUP BY ALL", [as_of, as_of, as_of, HORIZON_DAYS]).fetchall())


def baseline_receipts(con: duckdb.DuckDBPyConnection, as_of: date) -> int:
    """The naive forecast of a week's receipts: the receipts known on `as_of` and posted in the 13 weeks up to
    it, divided by 13 and rounded down."""
    (total,) = con.execute(
        f"SELECT coalesce(sum(amount_cents), 0) FROM ({BANK}) WHERE NOT is_opening AND amount_cents > 0 "
        "AND posted_on > ?::date - ? AND posted_on <= ?::date AND known_on <= ?::date",
        [as_of, HORIZON_DAYS, as_of, as_of]).fetchone()
    return int(total) // WEEKS


def revenue_error_bps(con: duckdb.DuckDBPyConnection, as_of: date, case: str) -> int | None:
    """|forecast − actual| ÷ actual in basis points over the three months after `as_of`'s month: the forecast
    revenue against the books' recognised revenue. None when the books' revenue does not cover them."""
    year, number = as_of.year, as_of.month
    wanted = []
    for _ in range(REVENUE_MONTHS):
        year, number = (year + 1, 1) if number == 12 else (year, number + 1)
        wanted.append(f"{year:04d}-{number:02d}")
    ((actual, covered),) = con.execute(
        "SELECT coalesce(sum(recognised_cents), 0), count(DISTINCT month) FROM mart.fct_revenue "
        "WHERE month IN (SELECT unnest(?))", [wanted]).fetchall()
    if covered < REVENUE_MONTHS or actual <= 0:
        return None
    (published,) = con.execute(
        'SELECT coalesce(sum(recognised_cents), 0) FROM forecast.revenue WHERE as_of = ? AND "case" = ? '
        "AND month IN (SELECT unnest(?))", [as_of, case, wanted]).fetchone()
    return abs(int(published) - int(actual)) * BPS // int(actual)


def score(con: duckdb.DuckDBPyConnection, as_of: date, cases: tuple[str, ...] = CASE_NAMES) -> list[dict]:
    """The metrics of the forecast published for `as_of`, one row per case, against the books."""
    actuals = actual_receipts(con, as_of)
    week = baseline_receipts(con, as_of)
    baseline = wape_bps([week] * WEEKS, actuals)
    (actual_net,) = con.execute(
        f"SELECT coalesce(sum(amount_cents), 0) FROM ({BANK}) WHERE NOT is_opening "
        "AND posted_on > ?::date AND posted_on <= ?::date + ?", [as_of, as_of, HORIZON_DAYS]).fetchone()
    (actual_balance,) = con.execute(
        f"SELECT coalesce(sum(amount_cents), 0) FROM ({BANK}) WHERE posted_on <= ?::date + ?",
        [as_of, HORIZON_DAYS]).fetchone()
    rows = []
    for case in cases:
        received = _weekly(con.execute(
            "SELECT (date - ?::date - 1) // 7, sum(cents) FROM forecast.daily WHERE as_of = ? AND \"case\" = ? "
            "AND direction = 'in' AND date > ?::date AND date <= ?::date + ? GROUP BY ALL",
            [as_of, as_of, case, as_of, as_of, HORIZON_DAYS]).fetchall())
        (net,) = con.execute(
            "SELECT coalesce(sum(CASE direction WHEN 'in' THEN cents ELSE -cents END), 0) FROM forecast.daily "
            "WHERE as_of = ? AND \"case\" = ? AND date > ?::date AND date <= ?::date + ?",
            [as_of, case, as_of, as_of, HORIZON_DAYS]).fetchone()
        (ending,) = con.execute(
            "SELECT DISTINCT ending_cents FROM forecast.weekly WHERE as_of = ? AND \"case\" = ? AND week = 'W13'",
            [as_of, case]).fetchone()
        rows.append({"as_of": as_of.isoformat(), "case": case,
                     "cash_in_wape_bps": wape_bps(received, actuals),
                     "net_cash_error_cents": int(net) - int(actual_net),
                     "ending_balance_error_cents": int(ending) - int(actual_balance),
                     "baseline_wape_bps": baseline,
                     "revenue_3m_error_bps": revenue_error_bps(con, as_of, case)})
    return rows


def write(con: duckdb.DuckDBPyConnection, as_of: date, rows: list[dict]) -> None:
    """Replace the `forecast.backtest` rows of `as_of` with `rows`, in one transaction."""
    con.execute("CREATE SCHEMA IF NOT EXISTS forecast")
    con.execute(f"CREATE TABLE IF NOT EXISTS forecast.backtest ({TABLE})")
    con.execute("BEGIN")
    try:
        con.execute("DELETE FROM forecast.backtest WHERE as_of = ?", [as_of])
        con.executemany("INSERT INTO forecast.backtest VALUES (?, ?, ?, ?, ?, ?, ?)",
                        [(as_of, r["case"], *(r[m] for m in METRICS)) for r in rows])
        con.execute("COMMIT")
    except BaseException:
        con.execute("ROLLBACK")
        raise


def check_closed(warehouse: str | Path, books: tuple[str, str], through: str) -> None:
    """Raise a precondition error unless every month from the books' first through `through` is closed
    and the warehouse holds the books to their last month, which the scoring reads."""
    first, last = books
    if not Path(warehouse).is_file():
        raise PreconditionError(f"month {first} not closed: no warehouse at {warehouse}; "
                                f"run `c2c close --month {last}` first.")
    con = db.connect(warehouse, read_only=True)
    try:
        for month in state.months(first, through):
            if state.status(con, month) not in (state.CLOSED, state.LOCKED):
                raise PreconditionError(f"month {month} not closed: run `c2c close --month {last}` first.")
        loaded = con.execute("SELECT max(_month) FROM raw.bank_transactions").fetchone()[0]
    finally:
        con.close()
    if loaded is None or loaded < last:
        raise PreconditionError(f"the books are loaded only through {loaded}, and the backtest scores against "
                                f"them to {last}: run `c2c close --month {last}` first.")


def run(warehouse: str | Path, policy: Policy, books: tuple[str, str], from_: str | None = None,
        to: str | None = None) -> dict:
    """Forecast as of the end of each eligible month of `books` (first and last month) from `from_` through
    `to`, through `forecast.run`, each published in its own transaction; score it against the books and
    write `forecast.backtest`. Returns `{"rows": [...], "skipped": [...]}`."""
    first, last = books
    months = [m for m in state.months(first, last) if (from_ or first) <= m <= (to or last)]
    eligible, skipped = split(books, months, policy)
    rows: list[dict] = []
    if eligible:
        check_closed(warehouse, books, eligible[-1])
    for month in eligible:
        as_of = forecast.month_end(month)
        forecast.run(warehouse, as_of, CASE_NAMES, policy)
        con = db.connect(warehouse)
        try:
            scored = score(con, as_of)
            write(con, as_of, scored)
        finally:
            con.close()
        rows += scored
    return {"rows": rows, "skipped": skipped}
