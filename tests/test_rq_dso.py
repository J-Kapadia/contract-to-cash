"""Days sales outstanding over the closed `mini` books.

Each month's receivables and billings are the sums of the receivables roll-forward, the days are the
floor of receivables times the days of the month over the billings, a month that billed nothing has
none, and every figure is a provenance record that `explain` resolves.
"""

from __future__ import annotations

import pytest

from c2c import db

from conftest import MINI_BOOKS, c2c
from forecast_helpers import mini_closed  # noqa: F401  (fixture)

MONTH = "2026-03"
EMPTY_MONTH = "2024-10"
BUCKETS = ["1-30", "31-60", "61-90", "91+", "current"]


def query(warehouse, sql, params=()):
    with db.connect(warehouse, read_only=True) as con:
        return con.execute(sql, params).fetchall()


def dso(warehouse):
    """Month -> (ar_closing_cents, billed_cents, days_in_month, dso_days)."""
    return {m: row for m, *row in query(
        warehouse, "SELECT month, ar_closing_cents, billed_cents, days_in_month, dso_days FROM mart.fct_dso")}


def test_every_closed_month_sums_the_roll_forward(mini_closed):
    sums = {m: (closing, billed) for m, closing, billed in query(
        mini_closed, "SELECT month, sum(closing_cents), sum(billed_cents) FROM mart.fct_ar_rollforward GROUP BY month")}
    rows = dso(mini_closed)
    assert rows and set(rows) == set(sums)
    for month, (closing, billed, *_) in rows.items():
        assert (closing, billed) == sums[month], month


def test_march_bills_and_counts_its_days(mini_closed):
    closing, billed, days_in_month, days = dso(mini_closed)[MONTH]
    assert (billed, days_in_month) == (35963450, 31)
    assert days == closing * days_in_month // billed


def test_a_month_without_an_invoice_has_no_days_and_no_record(mini_closed):
    # The roll-forward starts with the first month that billed, so the empty month has no row at all.
    assert dso(mini_closed).get(EMPTY_MONTH, (None,) * 4)[3] is None
    assert query(mini_closed, "SELECT count(*) FROM mart.fct_dso WHERE billed_cents <= 0 AND dso_days IS NOT NULL") == [(0,)]
    assert query(mini_closed, "SELECT number_id FROM prov.records WHERE number_id = ?", [f"dso/{EMPTY_MONTH}/days"]) == []


def test_every_month_with_days_has_a_record_of_them(mini_closed):
    expected = {m: row[3] for m, row in dso(mini_closed).items() if row[3] is not None}
    assert expected
    records = {m: (days, unit) for m, days, unit in query(
        mini_closed, "SELECT month, value_cents, unit FROM prov.records WHERE number_id = 'dso/' || month || '/days'")}
    assert records == {m: (days, "days") for m, days in expected.items()}


def test_explain_resolves_the_days_of_march(mini_closed):
    number_id = f"dso/{MONTH}/days"
    done = c2c("explain", number_id, "--json", "--books", str(MINI_BOOKS), "--warehouse", str(mini_closed))
    assert done.code == 0, done.stderr
    chain = done.json["explanation"]
    digest, count = query(mini_closed, "SELECT input_digest, input_row_count FROM prov.records WHERE number_id = ?", [number_id])[0]
    assert chain["record"]["number_id"] == number_id and chain["digest_matches"] is True
    assert (chain["derived_digest"], chain["derived_row_count"]) == (digest, count)


def test_the_aging_buckets_add_up_to_the_closing_receivables(mini_closed):
    totals = query(mini_closed, "SELECT number_id, value_cents FROM prov.records WHERE number_id = ANY(?)",
                   [[f"aging/{MONTH}/{bucket}" for bucket in BUCKETS]])
    assert sorted(n for n, _ in totals) == sorted(f"aging/{MONTH}/{bucket}" for bucket in BUCKETS)
    assert sum(cents for _, cents in totals) == dso(mini_closed)[MONTH][0]
