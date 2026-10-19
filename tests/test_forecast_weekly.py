"""The 13 forecast weeks, the commissions line and the minimum-cash alert.

The week-chaining rule is checked over random daily rows run through the models' own SQL (rendered
here with the tables stubbed), the opening balance against the bank balance computed from the books'
CSV file, and the alert on `mini` as of 2026-01-31.
"""

from __future__ import annotations

import csv
import shutil
from datetime import date, timedelta
from pathlib import Path

import jinja2
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

import macro_sql
from c2c import db
from conftest import MINI_BOOKS, ROOT
from forecast_helpers import forecast_built, mini_closed  # noqa: F401  (fixture)

AS_OF = date(2026, 1, 31)
CASES = ("base", "downside", "upside")
IN_LINES = ("open_ar", "contracted", "renewal", "expansion", "new")
OUT_LINES = ("payroll", "commissions", "cloud", "vendors", "events", "rent")
WEEKS = [f"W{n:02d}" for n in range(1, 14)]
MODELS = ROOT / "dbt" / "models" / "forecast"


def render(model: str, as_of: date, tables: dict[str, str]) -> str:
    """The SQL of a forecast model with `ref` mapped to scratch tables and the as-of date set."""
    env = jinja2.Environment(undefined=jinja2.StrictUndefined)
    return env.from_string((MODELS / f"{model}.sql").read_text()).render(
        ref=lambda name: tables[name],
        var=lambda name: {"as_of": as_of.isoformat()}[name],
        policy=lambda key: f"(select value from raw.ref_policy where key = '{key}')")


def query(warehouse, sql: str, params=()) -> list[tuple]:
    con = db.connect(warehouse, read_only=True)
    try:
        return con.execute(sql, params).fetchall()
    finally:
        con.close()


@pytest.fixture(scope="module")
def built(mini_closed, tmp_path_factory) -> Path:  # noqa: F811
    warehouse = tmp_path_factory.mktemp("weekly") / "w.duckdb"
    shutil.copyfile(mini_closed, warehouse)
    return forecast_built(warehouse, AS_OF)


# ------------------------------------------------------------------ the same SQL over random rows

SCRATCH = macro_sql._scratch_dir("c2c-weekly-")
_con = db.connect(SCRATCH / "weekly.duckdb")
_con.execute("CREATE SCHEMA IF NOT EXISTS raw")
WEEKLY_SQL = render("fc_cash_13w", AS_OF, {"fct_cash_daily": "daily_in", "fct_cash_rollforward": "rollforward_in"})
ALERTS_SQL = render("fc_alerts", AS_OF, {"fc_cash_13w": "weekly_out"})


def run_weekly(rows: list[tuple], opening: int) -> list[tuple]:
    """`(case, week, line, cents, opening_cents, ending_cents, week_start, week_end)` of `fc_cash_13w`
    over the given `(case, date, line, direction, cents)` rows and the closing cash of January."""
    _con.execute("CREATE OR REPLACE TABLE daily_in (as_of DATE, \"case\" VARCHAR, date DATE, line VARCHAR, "
                 "direction VARCHAR, cents BIGINT)")
    _con.execute("CREATE OR REPLACE TABLE rollforward_in (month VARCHAR, closing_cents BIGINT)")
    _con.execute("INSERT INTO rollforward_in VALUES ('2026-01', ?)", [opening])
    for case, day, line, direction, cents in rows:
        _con.execute("INSERT INTO daily_in VALUES (?, ?, ?, ?, ?, ?)", [AS_OF, case, day, line, direction, cents])
    _con.execute(f"CREATE OR REPLACE TABLE weekly_out AS {WEEKLY_SQL}")
    return _con.execute('SELECT "case", week, line, cents, opening_cents, ending_cents, week_start, week_end '
                        "FROM weekly_out ORDER BY 1, 2, 3").fetchall()


def run_alerts(minimum: int) -> list[tuple]:
    _con.execute("CREATE OR REPLACE TABLE raw.ref_policy (key VARCHAR, value BIGINT)")
    _con.execute("INSERT INTO raw.ref_policy VALUES ('min_cash_cents', ?)", [minimum])
    return _con.execute(f'SELECT "case", first_breach_week, shortfall_cents FROM ({ALERTS_SQL}) ORDER BY 1').fetchall()


daily_rows = st.lists(
    st.one_of(
        st.tuples(st.sampled_from(CASES), st.integers(-3, 95).map(lambda n: AS_OF + timedelta(days=n)),
                  st.sampled_from(IN_LINES), st.just("in"), st.integers(1, 5 * 10**8)),
        st.tuples(st.sampled_from(CASES), st.integers(-3, 95).map(lambda n: AS_OF + timedelta(days=n)),
                  st.sampled_from(OUT_LINES), st.just("out"), st.integers(1, 5 * 10**8))),
    max_size=60)


def expected_weeks(rows: list[tuple], case: str) -> dict[str, tuple[int, int]]:
    """`{week: (cash in, cash out)}` for the case, bucketed by 7 days from the day after the as-of date."""
    out = {week: [0, 0] for week in WEEKS}
    for k, day, _, direction, cents in rows:
        offset = (day - AS_OF).days - 1
        if k == case and 0 <= offset < 91:
            out[WEEKS[offset // 7]][0 if direction == "in" else 1] += cents
    return {week: (i, o) for week, (i, o) in out.items()}


@settings(max_examples=40, deadline=None)
@given(rows=daily_rows, opening=st.integers(-10**9, 10**10))
def test_every_week_ends_at_its_opening_plus_cash_in_minus_cash_out_and_the_next_opens_there(rows, opening):
    got = run_weekly(rows, opening)
    cases = {k for k, *_ in rows} or set()
    assert {r[0] for r in got} == cases
    for case in cases:
        weeks = expected_weeks(rows, case)
        balance = opening
        for week in WEEKS:
            lines = [r for r in got if r[0] == case and r[1] == week]
            assert len({(r[4], r[5]) for r in lines}) == 1, "opening and ending are the same on every line of a week"
            _, _, _, _, opens, ends, start, end = lines[0]
            cash_in, cash_out = weeks[week]
            assert opens == balance
            assert ends == opens + cash_in - cash_out
            assert sum(r[3] for r in lines if r[2] in IN_LINES) == cash_in
            assert sum(r[3] for r in lines if r[2] in OUT_LINES) == cash_out
            balance = ends
        assert [(r[6], r[7]) for r in got if r[0] == case and r[1] == "W01"][0] == (AS_OF + timedelta(days=1),
                                                                                  AS_OF + timedelta(days=7))
        assert [r[7] for r in got if r[0] == case and r[1] == "W13"][0] == AS_OF + timedelta(days=91)


@settings(max_examples=40, deadline=None)
@given(rows=daily_rows, opening=st.integers(0, 10**10), minimum=st.integers(0, 10**10))
def test_the_alert_names_the_first_week_ending_below_the_minimum_and_its_shortfall(rows, opening, minimum):
    got = run_weekly(rows, opening)
    ends = {(r[0], r[1]): r[5] for r in got}
    alerts = run_alerts(minimum)
    for case in {r[0] for r in got}:
        below = [week for week in WEEKS if ends[(case, week)] < minimum]
        want = [(case, below[0], minimum - ends[(case, below[0])])] if below else []
        assert [a for a in alerts if a[0] == case] == want


# ------------------------------------------------------------------ on mini as of 2026-01-31

def test_the_first_week_opens_at_the_bank_balance_on_the_as_of_date(built):
    with open(MINI_BOOKS / "bank_transactions.csv", newline="", encoding="utf-8") as handle:
        balance = sum(int(r["amount_cents"]) for r in csv.DictReader(handle) if date.fromisoformat(r["posted_on"]) <= AS_OF)
    rows = query(built, 'SELECT "case", opening_cents FROM fc_build.fc_cash_13w WHERE week = \'W01\' GROUP BY ALL')
    assert sorted(rows) == [(case, balance) for case in CASES]


def test_the_weeks_of_the_built_forecast_chain_in_every_case(built):
    for case in CASES:
        weeks = query(built, 'SELECT week, any_value(opening_cents), any_value(ending_cents), '
                             "sum(cents) FILTER (WHERE line IN ('open_ar', 'contracted', 'renewal', 'expansion', 'new')), "
                             "sum(cents) FILTER (WHERE line NOT IN ('open_ar', 'contracted', 'renewal', 'expansion', 'new')) "
                             'FROM fc_build.fc_cash_13w WHERE "case" = ? GROUP BY week ORDER BY week', [case])
        assert [w[0] for w in weeks] == WEEKS
        for week, previous in zip(weeks[1:], weeks):
            assert week[1] == previous[2]
        for _, opens, ends, cash_in, cash_out in weeks:
            assert ends == opens + cash_in - cash_out


def test_the_daily_cash_is_the_three_cash_models_with_commissions_as_their_own_line(built):
    daily = query(built, 'SELECT "case", line, direction, sum(cents) FROM fc_build.fct_cash_daily GROUP BY ALL ORDER BY ALL')
    parts = query(built, 'SELECT "case", line, \'in\', sum(cents) FROM fc_build.fc_cash_in GROUP BY ALL '
                         'UNION ALL SELECT "case", line, \'out\', sum(cents) FROM fc_build.fc_cash_out GROUP BY ALL '
                         'UNION ALL SELECT "case", \'commissions\', \'out\', sum(cents) FROM fc_build.fc_commissions GROUP BY ALL '
                         'ORDER BY ALL')
    assert daily == parts


def test_commissions_are_the_plan_rates_on_forecast_bookings_and_collections_paid_on_the_next_15th(built):
    with open(MINI_BOOKS / "commission_plan.csv", newline="", encoding="utf-8") as handle:
        plan = list(csv.DictReader(handle))
    assert len(plan) == 1
    booking, collection = int(plan[0]["rate_on_booking_bps"]), int(plan[0]["rate_on_collection_bps"])
    want: dict[tuple[str, date], int] = {}

    def add(case: str, day: date, cents: int) -> None:
        pay = (day.replace(day=1) + timedelta(days=32)).replace(day=15)
        if AS_OF < pay <= AS_OF + timedelta(days=91):
            want[(case, pay)] = want.get((case, pay), 0) + cents

    for case, start, fee in query(built, 'SELECT "case", term_start, annual_fee_cents FROM fc_build.fc_contracts_renewal '
                                         'UNION ALL SELECT "case", term_start, annual_fee_cents FROM fc_build.fc_contracts_pipeline'):
        add(case, start, fee * booking // 10000)
    for case, day, cents in query(built, 'SELECT "case", date, cents FROM fc_build.fc_cash_in '
                                         "WHERE line IN ('renewal', 'expansion', 'new')"):
        add(case, day, cents * collection // 10000)
    got = {(case, day): cents for case, day, cents in query(built, 'SELECT "case", date, cents FROM fc_build.fc_commissions')}
    assert want and got == {k: v for k, v in want.items() if v}
    assert {day.day for _, day in got} == {15}
