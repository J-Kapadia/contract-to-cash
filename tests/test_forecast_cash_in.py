"""Forecast cash in on `mini` as of 2026-01-31: open invoices and forecast invoices by line.

The open-invoice line must be what the close's expected cash says for the month of the as-of
date that falls in the horizon; every date must fall in the 91 days after it, on a business day;
a forecast invoice is dated from its customer's days to pay (the pooled ones when it has none);
and a slower-paying case never collects earlier than a faster one.
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest

import test_single_definition
from c2c import db
from forecast_helpers import forecast_built, mini_closed, mini_closed_copy  # noqa: F401  (fixtures)

AS_OF = date(2026, 1, 31)
CASES = ("base", "downside", "upside")
LINES = {"open_ar", "contracted", "renewal", "expansion", "new"}


@pytest.fixture
def built(mini_closed_copy) -> object:  # noqa: F811
    return forecast_built(mini_closed_copy, AS_OF)


def query(warehouse, sql: str, params=()) -> list[tuple]:
    con = db.connect(warehouse, read_only=True)
    try:
        return con.execute(sql, params).fetchall()
    finally:
        con.close()


def horizon_end() -> date:
    return AS_OF + timedelta(days=91)


def test_the_open_ar_line_is_the_close_expected_cash_by_case_within_the_horizon(built):
    got = dict(query(built, "SELECT \"case\", sum(cents) FROM fc_build.fc_cash_in WHERE line = 'open_ar' GROUP BY 1"))
    want = dict(query(built, "SELECT \"case\", sum(cents) FROM mart.fct_expected_cash "
                             "WHERE month = '2026-01' AND expected_date <= ? GROUP BY 1", [horizon_end()]))
    assert set(got) == set(CASES)
    assert got == want

    by_date = query(built, "SELECT \"case\", date, sum(cents) FROM fc_build.fc_cash_in WHERE line = 'open_ar' GROUP BY 1, 2")
    expected = query(built, "SELECT \"case\", expected_date, sum(cents) FROM mart.fct_expected_cash "
                            "WHERE month = '2026-01' AND expected_date <= ? AND cents <> 0 GROUP BY 1, 2", [horizon_end()])
    assert sorted(by_date) == sorted(expected)


def test_cash_expected_after_the_horizon_is_left_out_not_moved(built):
    beyond = query(built, "SELECT \"case\", sum(cents) FROM mart.fct_expected_cash "
                          "WHERE month = '2026-01' AND expected_date > ? GROUP BY 1", [horizon_end()])
    assert beyond, "the downside has an invoice expected after the horizon on mini"
    total = dict(query(built, "SELECT \"case\", sum(cents) FROM mart.fct_expected_cash WHERE month = '2026-01' GROUP BY 1"))
    inside = dict(query(built, "SELECT \"case\", sum(cents) FROM fc_build.fc_cash_in WHERE line = 'open_ar' GROUP BY 1"))
    assert {case: total[case] - inside[case] for case in total} == {case: dict(beyond).get(case, 0) for case in total}


def test_every_row_is_in_the_horizon_on_a_business_day_with_a_known_line(built):
    rows = query(built, 'SELECT as_of, "case", date, line, cents FROM fc_build.fc_cash_in')
    assert rows
    assert {r[0] for r in rows} == {AS_OF}
    assert {r[1] for r in rows} == set(CASES)
    assert {r[3] for r in rows} <= LINES and {"open_ar", "contracted"} <= {r[3] for r in rows}
    assert all(AS_OF + timedelta(days=1) <= r[2] <= horizon_end() for r in rows)
    assert all(r[4] != 0 for r in rows)
    assert len({r[1:4] for r in rows}) == len(rows), "one row per case, day and line"
    business = {d for (d,) in query(built, "SELECT \"date\"::date FROM raw.ref_business_days WHERE is_business_day::int = 1")}
    assert {r[2] for r in rows} <= business


def cumulative(rows: list[tuple], case: str, line: str, day: date) -> int:
    return sum(c for k, d, ln, c in rows if k == case and ln == line and d <= day)


@pytest.mark.parametrize("line", ["open_ar", "contracted"])
def test_the_downside_never_collects_earlier_than_the_base(built, line):
    rows = query(built, 'SELECT "case", date, line, cents FROM fc_build.fc_cash_in')
    days = sorted({d for _, d, ln, _ in rows if ln == line})
    assert days
    for day in days:
        assert cumulative(rows, "downside", line, day) <= cumulative(rows, "base", line, day), day


def test_each_open_invoice_is_dated_no_earlier_in_the_downside_than_in_the_base(built):
    late = query(built, "SELECT count(*) FROM mart.fct_expected_cash d JOIN mart.fct_expected_cash b "
                        "ON b.month = d.month AND b.invoice_id = d.invoice_id AND b.\"case\" = 'base' "
                        "WHERE d.month = '2026-01' AND d.\"case\" = 'downside' AND d.expected_date < b.expected_date")
    assert late == [(0,)]


def test_a_contracted_invoice_is_dated_from_its_customers_days_to_pay_or_the_pooled_ones(built):
    days = {(c, d) for c, d in query(built, "SELECT customer_id, median_days FROM mart.fct_days_to_pay WHERE month = '2026-01'")}
    p80 = dict(query(built, "SELECT customer_id, p80_days FROM mart.fct_days_to_pay WHERE month = '2026-01'"))
    median = dict(days)
    business = sorted(d for (d,) in query(built, "SELECT \"date\"::date FROM raw.ref_business_days WHERE is_business_day::int = 1"))
    invoices = query(built, "SELECT \"case\", customer_id, invoice_date, sum(amount_cents) FROM fc_build.fc_billing_schedule "
                            "WHERE source = 'contracted' AND invoice_date > ? GROUP BY ALL", [AS_OF])
    assert invoices

    want: dict[tuple[str, date], int] = {}
    for case, customer, invoice_date, cents in invoices:
        table = p80 if case == "downside" else median
        nominal = invoice_date + timedelta(days=table.get(customer, table["*"]))
        pay = min(d for d in business if d >= nominal)
        if pay <= horizon_end():
            want[(case, pay)] = want.get((case, pay), 0) + cents
    got = {(case, d): c for case, d, c in query(built, "SELECT \"case\", date, cents FROM fc_build.fc_cash_in WHERE line = 'contracted'")}
    assert got == want


def test_an_invoice_dated_on_or_before_the_as_of_date_is_not_read_from_the_schedule(built):
    dated = query(built, "SELECT count(*) FROM fc_build.fc_billing_schedule WHERE invoice_date <= ?", [AS_OF])
    assert dated[0][0] > 0, "the schedule holds invoices the books already carry"
    total = query(built, "SELECT sum(cents) FROM fc_build.fc_cash_in WHERE line = 'contracted' AND \"case\" = 'base'")
    future = query(built, "SELECT sum(amount_cents) FROM fc_build.fc_billing_schedule "
                          "WHERE source = 'contracted' AND \"case\" = 'base' AND invoice_date > ?", [AS_OF])
    assert total[0][0] <= future[0][0]


def test_the_model_has_no_second_definition_of_an_expected_cash_date():
    path = test_single_definition.MODELS / "forecast" / "fc_cash_in.sql"
    assert "expected_pay_date(" in path.read_text()
    assert not test_single_definition.violations(path.read_text())
