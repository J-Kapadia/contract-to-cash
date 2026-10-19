"""Pipeline forecast contracts on `mini`: one row per open new or expansion opportunity, case and as-of.

The expected rows are worked out here from the books' CSV files and the staged inputs, not from the
SQL under test.
"""

from __future__ import annotations

import csv
from datetime import date, timedelta

from c2c import db
from conftest import MINI_BOOKS
from forecast_helpers import forecast_built, mini_closed, mini_closed_copy  # noqa: F401  (fixtures)

AS_OF = date(2026, 3, 31)
CASES = ("base", "downside", "upside")
OPEN = ("S1", "S2", "S3", "S4", "S5")
STALE_SHIFT_DAYS = 28
STALE_WEIGHT_BPS = 5000
STALE_OPP = "opp_0002_x01"


def snapshots() -> list[dict]:
    with open(MINI_BOOKS / "pipeline_snapshots.csv", newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def day(text: str) -> date:
    return date.fromisoformat(text)


def add_months(d: date, months: int) -> date:
    total = d.year * 12 + d.month - 1 + months
    year, month = divmod(total, 12)
    next_first = date(year + (month + 1) // 12, (month + 1) % 12 + 1, 1)
    return min(d.replace(year=year, month=month + 1, day=1) + timedelta(days=d.day - 1), next_first - timedelta(days=1))


def open_in_latest(as_of: date = AS_OF) -> list[dict]:
    known = [r for r in snapshots() if day(r["snapshot_date"]) <= as_of]
    latest = max(r["snapshot_date"] for r in known)
    return [r for r in known if r["snapshot_date"] == latest and r["stage"] in OPEN]


def rows(warehouse) -> list[tuple]:
    con = db.connect(warehouse, read_only=True)
    try:
        return con.execute('SELECT as_of, "case", source, customer_id, opp_id, term_start, term_end, '
                           "billing_frequency, annual_fee_cents, stale FROM fc_build.fc_contracts_pipeline "
                           'ORDER BY "case", opp_id').fetchall()
    finally:
        con.close()


def inputs(warehouse) -> dict[tuple[str, str], int]:
    """`{(case, driver): value}` as staged for the build."""
    con = db.connect(warehouse, read_only=True)
    try:
        return {(case, driver): value for case, driver, value in
                con.execute('SELECT "case", driver, value FROM fc_build.inputs').fetchall()}
    finally:
        con.close()


def test_every_open_new_or_expansion_opportunity_yields_a_row_per_case(mini_closed_copy):
    forecast_built(mini_closed_copy, AS_OF)
    got = {(r[1], r[4]): r for r in rows(mini_closed_copy)}
    staged = inputs(mini_closed_copy)

    opps = [r for r in open_in_latest() if r["opp_type"] != "renewal"]
    assert {r["opp_id"] for r in opps} == {"opp_0001_x01", "opp_0002_x01", "opp_0007_x01"}
    assert len(got) == len(opps) * len(CASES)
    for case in CASES:
        for o in opps:
            stale = day(o["expected_close_date"]) < AS_OF
            start = AS_OF + timedelta(days=STALE_SHIFT_DAYS) if stale else day(o["expected_close_date"])
            start += timedelta(days=staged[(case, "pipeline_shift_days")])
            weight = staged[(case, f"rate_win_{o['stage']}")]
            if stale:
                weight = weight * STALE_WEIGHT_BPS // 10000
            row = got[(case, o["opp_id"])]
            assert row[0] == AS_OF
            assert row[2] == o["opp_type"]
            assert row[3] == o["crm_account_id"]
            assert row[5] == start
            assert row[6] == add_months(start, int(o["term_months"])) - timedelta(days=1)
            assert row[7] == o["billing_frequency"]
            assert row[8] == int(o["acv_cents"]) * weight // 10000
            assert row[9] is stale


def test_the_stale_opportunity_starts_four_weeks_after_the_as_of_date_at_half_weight(mini_closed_copy):
    forecast_built(mini_closed_copy, AS_OF)
    got = {(r[1], r[4]): r for r in rows(mini_closed_copy)}
    staged = inputs(mini_closed_copy)
    stale = next(r for r in snapshots() if r["opp_id"] == STALE_OPP and r["snapshot_date"] == "2026-03-31")

    base = got[("base", STALE_OPP)]
    assert stale["stage"] == "S4" and day(stale["expected_close_date"]) < AS_OF
    assert base[5] == AS_OF + timedelta(days=28) == date(2026, 4, 28)
    assert base[9] is True
    full = int(stale["acv_cents"]) * staged[("base", "rate_win_S4")] // 10000
    assert base[8] == int(stale["acv_cents"]) * (staged[("base", "rate_win_S4")] // 2) // 10000
    assert base[8] * 2 <= full
    assert {(r[1], r[4]) for r in rows(mini_closed_copy) if r[9]} == {(case, STALE_OPP) for case in CASES}


def test_renewal_opportunities_contribute_nothing(mini_closed_copy):
    forecast_built(mini_closed_copy, AS_OF)
    got = rows(mini_closed_copy)

    renewals = [r for r in open_in_latest() if r["opp_type"] == "renewal"]
    assert renewals, "the books carry an open renewal opportunity"
    assert {r[4] for r in got}.isdisjoint(r["opp_id"] for r in renewals)
    assert {r[2] for r in got} == {"expansion"}


def test_the_downside_shifts_the_start_thirty_days_and_halves_the_win_rate(mini_closed_copy):
    forecast_built(mini_closed_copy, AS_OF)
    got = {(r[1], r[4]): r for r in rows(mini_closed_copy)}
    staged = inputs(mini_closed_copy)

    terms = {r["opp_id"]: int(r["term_months"]) for r in open_in_latest()}
    for opp in ("opp_0001_x01", "opp_0007_x01", STALE_OPP):
        base, down, up = (got[(case, opp)] for case in CASES)
        assert down[5] - base[5] == timedelta(days=30)
        assert down[6] == add_months(down[5], terms[opp]) - timedelta(days=1)
        assert up[5] == base[5]
    stage = next(r["stage"] for r in open_in_latest() if r["opp_id"] == "opp_0001_x01")
    assert staged[("downside", f"rate_win_{stage}")] == staged[("base", f"rate_win_{stage}")] * 5000 // 10000
    assert staged[("upside", f"rate_win_{stage}")] == min(staged[("base", f"rate_win_{stage}")] * 12500 // 10000, 10000)
    assert got[("upside", "opp_0001_x01")][8] >= got[("base", "opp_0001_x01")][8] >= got[("downside", "opp_0001_x01")][8]


def test_a_snapshot_known_after_the_as_of_date_is_ignored(mini_closed_copy):
    forecast_built(mini_closed_copy, AS_OF)
    before = rows(mini_closed_copy)

    con = db.connect(mini_closed_copy)
    try:
        con.execute("INSERT INTO raw.pipeline_snapshots SELECT * REPLACE ('2026-04-30' AS snapshot_date, "
                    "'2026-04-30' AS known_on, 'h-late-1' AS _row_hash, 'S5' AS stage) FROM raw.pipeline_snapshots "
                    f"WHERE opp_id = '{STALE_OPP}' AND snapshot_date = '2026-03-31'")
        con.execute("INSERT INTO raw.pipeline_snapshots SELECT * REPLACE ('2026-04-30' AS snapshot_date, "
                    "'2026-04-30' AS known_on, 'h-late-2' AS _row_hash, 'opp_late' AS opp_id) FROM raw.pipeline_snapshots "
                    "WHERE opp_id = 'opp_0001_x01' AND snapshot_date = '2026-03-31'")
    finally:
        con.close()
    forecast_built(mini_closed_copy, AS_OF)

    assert rows(mini_closed_copy) == before
    assert "opp_late" not in {r[4] for r in rows(mini_closed_copy)}


def test_the_rollup_counts_the_stale_opportunities_of_each_month_end_snapshot(mini_closed):
    con = db.connect(mini_closed, read_only=True)
    try:
        got = dict(con.execute("SELECT month, value_cents FROM mart.fct_rollup WHERE metric = 'stale_opps'").fetchall())
    finally:
        con.close()

    expected: dict[str, int] = {}
    for r in snapshots():
        month = r["snapshot_date"][:7]
        stale = r["stage"] in OPEN and r["opp_type"] != "renewal" and r["expected_close_date"] < r["snapshot_date"]
        expected[month] = expected.get(month, 0) + stale
    assert got == expected
    assert got["2026-03"] == 1
