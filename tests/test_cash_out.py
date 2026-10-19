"""Forecast cash out on `mini`: payroll, cloud, vendors, events and rent as known on the as-of date.

The expectations are worked out here from the books' CSV files and the calendar (weekends are
not business days), not from the SQL under test.
"""

from __future__ import annotations

import csv
from collections import defaultdict
from datetime import date, timedelta

from c2c import db
from conftest import MINI_BOOKS
from forecast_helpers import forecast_built, mini_closed, mini_closed_copy  # noqa: F401  (fixtures)

AS_OF = date(2026, 1, 31)
CASES = ("base", "downside", "upside")
LINE_OF = {"cloud": "cloud", "events": "events", "rent": "rent", "vendor": "vendors", "software": "vendors"}


def books(name: str) -> list[dict]:
    with open(MINI_BOOKS / f"{name}.csv", newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def day(text: str) -> date:
    return date.fromisoformat(text)


def previous_business_day(d: date) -> date:
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return d


def month_end(d: date) -> date:
    return (d.replace(day=28) + timedelta(days=4)).replace(day=1) - timedelta(days=1)


def horizon(as_of: date) -> tuple[date, date]:
    return as_of + timedelta(days=1), as_of + timedelta(days=91)


def cash_out(warehouse, as_of: date = AS_OF) -> dict[tuple[str, date, str], int]:
    """`{(case, date, line): cents}` from the built `fc_build.fc_cash_out`."""
    con = db.connect(warehouse, read_only=True)
    try:
        rows = con.execute('SELECT as_of, "case", date, line, cents FROM fc_build.fc_cash_out').fetchall()
    finally:
        con.close()
    assert {r[0] for r in rows} == {as_of}
    out = {(r[1], r[2], r[3]): r[4] for r in rows}
    assert len(out) == len(rows), "one row per case, day and line"
    return out


def seed(warehouse, table: str, **changes: str) -> None:
    """Copy the first row of a raw books table with the given columns changed."""
    replaced = ", ".join(f"'{value}' AS {column}" for column, value in changes.items())
    con = db.connect(warehouse)
    try:
        con.execute(f"INSERT INTO raw.{table} SELECT * REPLACE ({replaced}) FROM raw.{table} LIMIT 1")
    finally:
        con.close()


def payroll_rows(rows: dict, case: str = "base") -> dict[date, int]:
    return {d: cents for (c, d, line), cents in rows.items() if c == case and line == "payroll"}


def expected_payroll(as_of: date, extra_deltas: tuple[tuple[str, int], ...] = ()) -> dict[date, int]:
    runs = [r for r in books("payroll_runs") if day(r["known_on"]) <= as_of and day(r["pay_date"]) <= as_of]
    last = {half: max((r for r in runs if r["half"] == half), key=lambda r: r["pay_date"]) for half in ("mid", "end")}
    deltas = [(r["month"], int(r["delta_cash_per_run_cents"])) for r in books("headcount_plan")
              if day(r["known_on"]) <= as_of] + list(extra_deltas)
    first, final = horizon(as_of)
    out = {}
    for offset in range(5):
        year, month = divmod(as_of.year * 12 + as_of.month - 1 + offset, 12)
        start = date(year, month + 1, 1)
        for half, nominal in (("mid", start.replace(day=15)), ("end", month_end(start))):
            paid = previous_business_day(nominal)
            if first <= paid <= final:
                since = last[half]["pay_date"][:7]
                out[paid] = int(last[half]["cash_cents"]) + sum(
                    cents for m, cents in deltas if since < m <= start.isoformat()[:7])
    return out


def open_payables(as_of: date) -> dict[tuple[date, str], int]:
    """`{(date, line): cents}` for the bills unpaid at `as_of` and the commitments no known bill covers."""
    bank = {r["bank_txn_id"]: day(r["posted_on"]) for r in books("bank_transactions") if day(r["known_on"]) <= as_of}
    bills = [b for b in books("ap_bills") if day(b["known_on"]) <= as_of]
    first, final = horizon(as_of)
    out: dict[tuple[date, str], int] = defaultdict(int)
    for b in bills:
        paid = bank.get(b["paid_bank_txn_id"])
        if paid is None or paid > as_of:
            pay = max(day(b["due_date"]), first)
            if pay <= final:
                out[(pay, LINE_OF[b["category"]])] += int(b["amount_cents"])
    covered = {(b["vendor_name"], b["category"], b["due_date"]) for b in bills}
    for c in books("vendor_commitments"):
        if (day(c["known_on"]) <= as_of and first <= day(c["due_date"]) <= final
                and (c["vendor_name"], c["category"], c["due_date"]) not in covered):
            out[(day(c["due_date"]), LINE_OF[c["category"]])] += int(c["amount_cents"])
    return out


def trailing_cloud_mean(as_of: date, months: int = 3) -> int:
    billed: dict[str, int] = defaultdict(int)
    for b in books("ap_bills"):
        if b["category"] == "cloud" and day(b["known_on"]) <= as_of:
            billed[b["bill_date"][:7]] += int(b["amount_cents"])
    recent = [billed[m] for m in sorted(billed)[-months:]]
    return sum(recent) // len(recent)


def test_payroll_runs_on_the_shifted_15th_and_month_end_at_the_last_actual_run(mini_closed_copy):
    forecast_built(mini_closed_copy, AS_OF)
    rows = cash_out(mini_closed_copy)

    expected = expected_payroll(AS_OF)
    assert sorted(expected) == [date(2026, 2, 13), date(2026, 2, 27), date(2026, 3, 13),
                                date(2026, 3, 31), date(2026, 4, 15), date(2026, 4, 30)]
    assert set(expected.values()) == {10920000}
    for case in CASES:
        assert payroll_rows(rows, case) == expected


def test_a_headcount_delta_known_after_the_as_of_date_is_ignored(mini_closed_copy):
    seed(mini_closed_copy, "headcount_plan", month="2026-03", delta_cash_per_run_cents="500000",
         known_on="2026-02-15")
    forecast_built(mini_closed_copy, AS_OF)

    assert payroll_rows(cash_out(mini_closed_copy)) == expected_payroll(AS_OF)


def test_a_headcount_delta_known_by_the_as_of_date_raises_the_runs_from_its_month(mini_closed_copy):
    seed(mini_closed_copy, "headcount_plan", month="2026-03", delta_cash_per_run_cents="500000",
         known_on="2026-01-20")
    forecast_built(mini_closed_copy, AS_OF)

    payroll = payroll_rows(cash_out(mini_closed_copy))
    assert payroll == expected_payroll(AS_OF, extra_deltas=(("2026-03", 500000),))
    assert payroll[date(2026, 2, 27)] == 10920000
    assert payroll[date(2026, 3, 13)] == 11420000


def test_cloud_is_the_trailing_three_month_mean_paid_30_days_after_month_end_and_up_in_the_downside(
        mini_closed_copy):
    forecast_built(mini_closed_copy, AS_OF)
    rows = cash_out(mini_closed_copy)

    mean = trailing_cloud_mean(AS_OF)
    assert mean == (6754150 + 5937100 + 6618300) // 3
    for case, bps in (("base", 0), ("downside", 1000), ("upside", 0)):
        accrued = mean * (10000 + bps) // 10000
        assert {d: c for (k, d, line), c in rows.items() if k == case and line == "cloud"} == {
            date(2026, 3, 2): 6618300,  # January's bill, billed on A and paid after it
            date(2026, 3, 30): accrued,  # February accrued, paid net-30
            date(2026, 4, 30): accrued,  # March accrued, paid net-30
        }


def test_every_open_bill_due_in_the_horizon_appears_once_and_a_later_known_bill_does_not(mini_closed_copy):
    seed(mini_closed_copy, "ap_bills", bill_id="bill_later", category="vendor", due_date="2026-03-01",
         bill_date="2026-02-10", amount_cents="777777", paid_bank_txn_id="", known_on="2026-02-10")
    forecast_built(mini_closed_copy, AS_OF)
    rows = cash_out(mini_closed_copy)

    expected = open_payables(AS_OF)
    assert expected[(date(2026, 3, 3), "vendors")] == 9600000  # the software bill, paid after A
    for case in CASES:
        got = {(d, line): c for (k, d, line), c in rows.items() if k == case and line in {"vendors", "events", "rent"}}
        assert got == {k: v for k, v in expected.items() if k[1] != "cloud"}
        cloud = {d: c for (k, d, line), c in rows.items() if k == case and line == "cloud"}
        assert cloud[date(2026, 3, 2)] == expected[(date(2026, 3, 2), "cloud")]


def test_a_commitment_a_known_bill_covers_is_paid_once(mini_closed_copy):
    as_of = date(2026, 2, 5)  # the rent bill due 02-06 is known and so is its commitment
    forecast_built(mini_closed_copy, as_of)
    rows = cash_out(mini_closed_copy, as_of)

    assert rows[("base", date(2026, 2, 6), "rent")] == 1800000
    expected = open_payables(as_of)
    got = {(d, line): c for (k, d, line), c in rows.items() if k == "base" and line in {"vendors", "events", "rent"}}
    assert got == {k: v for k, v in expected.items() if k[1] != "cloud"}
    assert all(first <= d <= last for first, last in [horizon(as_of)] for (_, d, _) in rows)
