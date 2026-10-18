"""The AR roll-forward and its tie-out to GL 1200 on the `small` books, closed through 2026-03."""

from __future__ import annotations

import csv
from collections import defaultdict

from c2c import db

MONTH = "2026-03"


def rows(path, name):
    with (path / f"{name}.csv").open(encoding="utf-8", newline="") as fh:
        return list(csv.DictReader(fh))


def handmade_closing(books) -> dict[str, int]:
    """Receivable per CRM account at the end of March: what was billed less what was applied."""
    account = {c["contract_id"]: c["crm_account_id"] for c in rows(books, "contracts")}
    invoice_account = {i["invoice_id"]: account[i["contract_id"]] for i in rows(books, "invoices")}
    closing: dict[str, int] = defaultdict(int)
    for invoice in rows(books, "invoices"):
        closing[account[invoice["contract_id"]]] += int(invoice["amount_cents"])
    for payment in rows(books, "payments"):
        closing[invoice_account[payment["invoice_id"]]] -= int(payment["applied_cents"])
    return dict(closing)


def query(closed, sql, params=()):
    with db.connect(closed.warehouse, read_only=True) as con:
        return con.execute(sql, params).fetchall()


def test_closing_ar_per_customer_equals_the_handmade_figure(closed):
    expected = handmade_closing(closed.books)
    got = dict(query(closed, "SELECT customer_id, closing_cents FROM mart.fct_ar_rollforward WHERE month = ?",
                     [MONTH]))
    assert got == expected
    assert expected["acct_0001"] == 0  # paid in full in February
    assert expected["acct_0003"] > 0  # the part payer still owes


def test_rollforward_rows_add_up(closed):
    bad = query(closed, "SELECT count(*) FROM mart.fct_ar_rollforward"
                        " WHERE opening_cents + billed_cents - collected_cents - credits_cents != closing_cents")
    assert bad == [(0,)]
    opening = query(closed, "SELECT customer_id, opening_cents FROM mart.fct_ar_rollforward WHERE month = '2026-01'")
    assert {cents for _, cents in opening} == {0}


def test_gl_1200_ties_to_the_subledger_every_month(closed):
    got = query(closed, "SELECT month, gl_account, diff_cents FROM mart.fct_gl_tieout ORDER BY month")
    assert got == [("2026-01", "1200", 0), ("2026-02", "1200", 0), ("2026-03", "1200", 0)]
    assert query(closed, "SELECT gl_cents FROM mart.fct_gl_tieout WHERE month = ?", [MONTH]) == [
        (sum(handmade_closing(closed.books).values()),)]
