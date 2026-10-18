"""The receivables roll-forward and cash application over the `mini` books.

Every customer-month equals the expected roll-forward, partial, over and multi-invoice payments land
where the books put them, a receipt with no payment row is applied or left unmatched by its counterparty
and amount, and the customers' closings add up to the ledger's receivables balance.
"""

from __future__ import annotations

import csv
import shutil
from pathlib import Path

import pytest

from c2c import db, dbt_runner, load
from conftest import DEFAULT_CONFIG, MINI_BOOKS, MINI_WINDOW, ROOT, shared_dir

REFERENCE = ROOT / "data" / "reference"
EXPECTED = ROOT / "tests" / "fixtures" / "expected" / "rollforward.csv"


def build(warehouse: Path, books: Path = MINI_BOOKS) -> None:
    con = db.connect(warehouse)
    try:
        db.init_schemas(con)
        load.load_months(con, books, *MINI_WINDOW, "test")
        load.load_reference(con, REFERENCE)
        load.load_config(con, DEFAULT_CONFIG)
    finally:
        con.close()
    dbt_runner.build(warehouse, select="+fct_ar_rollforward")


def query(warehouse: Path, sql: str, params=()):
    con = db.connect(warehouse, read_only=True)
    try:
        return con.execute(sql, params).fetchall()
    finally:
        con.close()


def csv_rows(path: Path) -> list[dict]:
    with path.open(encoding="utf-8", newline="") as fh:
        return list(csv.DictReader(fh))


@pytest.fixture(scope="module")
def mini_ar(tmp_path_factory) -> Path:
    """The `mini` books loaded and the receivables roll-forward built (with its dbt tests). Read-only."""
    return shared_dir(tmp_path_factory, "ar-mini", lambda directory: build(directory / "w.duckdb")) / "w.duckdb"


def rollforward(warehouse: Path) -> list[tuple]:
    return query(warehouse, "SELECT month, customer_id, opening_cents, additions_cents, reductions_cents,"
                            " closing_cents FROM mart.fct_ar_rollforward ORDER BY month, customer_id")


def test_the_mart_carries_the_standard_columns(mini_ar):
    columns = [row[0] for row in query(
        mini_ar, "SELECT column_name FROM information_schema.columns"
                 " WHERE table_schema = 'mart' AND table_name = 'fct_ar_rollforward' ORDER BY ordinal_position")]
    assert {"month", "customer_id", "opening_cents", "additions_cents", "reductions_cents",
            "closing_cents"} <= set(columns)


def test_every_customer_month_equals_the_expected_rollforward(mini_ar):
    got = {(customer, month): (opening, additions, reductions, closing)
           for month, customer, opening, additions, reductions, closing in rollforward(mini_ar)}
    expected = {(r["customer_id"], r["month"]): tuple(int(r[f"ar_{part}_cents"]) for part in
                                                      ("opening", "additions", "reductions", "closing"))
                for r in csv_rows(EXPECTED)}
    assert sum(closing for *_, closing in expected.values()) > 0
    assert got == expected


def test_each_month_opens_where_the_previous_one_closed(mini_ar):
    closing: dict[str, int] = {}
    for month, customer, opening, additions, reductions, close in rollforward(mini_ar):
        assert opening == closing.get(customer, 0), (customer, month)
        assert opening + additions - reductions == close, (customer, month)
        closing[customer] = close


def test_the_customers_closings_add_up_to_the_ledger_receivables(mini_ar):
    ledger: dict[str, int] = {}
    for row in csv_rows(MINI_BOOKS / "gl.csv"):
        if row["gl_account"] == "1200":
            ledger[row["gl_period"]] = ledger.get(row["gl_period"], 0) + int(row["amount_cents"])
    totals: dict[str, int] = {}
    for month, *_, closing in rollforward(mini_ar):
        totals[month] = totals.get(month, 0) + closing
    balance = 0
    for month in sorted(totals):
        balance += ledger.get(month, 0)
        assert totals[month] == balance, month


# ------------------------------------------------------------------ payments of every shape


def applications(warehouse: Path, where: str, params=()) -> list[tuple]:
    return query(warehouse, "SELECT bank_txn_id, payment_id, invoice_id, customer_id, applied_cents, kind"
                            f" FROM int.int_cash_application WHERE {where} ORDER BY payment_id, invoice_id", params)


def test_an_invoice_paid_in_two_instalments_has_two_applications_that_add_up(mini_ar):
    rows = applications(mini_ar, "invoice_id = 'inv_000011'")
    assert len(rows) == 2 and {kind for *_, kind in rows} == {"applied"}
    invoiced = query(mini_ar, "SELECT sum(amount_cents) FROM int.stg_invoices WHERE invoice_id = 'inv_000011'")[0][0]
    assert sum(cents for *_, cents, _ in rows) == invoiced


def test_an_overpayment_is_applied_in_full_and_leaves_the_customer_with_negative_receivables(mini_ar):
    (_, _, _, customer, applied, kind), = applications(mini_ar, "invoice_id = 'inv_000016'")
    invoiced = query(mini_ar, "SELECT sum(amount_cents) FROM int.stg_invoices WHERE invoice_id = 'inv_000016'")[0][0]
    assert kind == "applied" and applied > invoiced
    closings = [closing for _, who, *_, closing in rollforward(mini_ar) if who == customer]
    assert closings[-1] == invoiced - applied < 0


def test_one_receipt_paying_two_invoices_applies_each_part_to_its_invoice(mini_ar):
    rows = applications(mini_ar, "bank_txn_id = 'bk_000071'")
    assert [(invoice, cents) for _, _, invoice, _, cents, _ in rows] == [("inv_000068", 17612500),
                                                                         ("inv_000072", 13650000)]
    assert len({customer for _, _, _, customer, _, _ in rows}) == 1
    receipt = query(mini_ar, "SELECT amount_cents FROM int.stg_bank_transactions WHERE bank_txn_id = 'bk_000071'")
    assert sum(cents for *_, cents, _ in rows) == receipt[0][0]


def test_every_payment_row_is_applied_once_and_the_mini_receipts_all_have_one(mini_ar):
    payments = query(mini_ar, "SELECT count(*), sum(applied_cents) FROM int.stg_payments")[0]
    assert query(mini_ar, "SELECT count(*), sum(applied_cents) FROM int.int_cash_application"
                          " WHERE kind = 'applied'")[0] == payments
    assert query(mini_ar, "SELECT count(*) FROM int.int_cash_application WHERE kind <> 'applied'") == [(0,)]


# ------------------------------------------------------------------ receipts with no payment row

PAYER = "Bramble Robotics Limited"  # a bank-statement spelling of acct_0001, billing customer bill_0001
EXTRA_RECEIPTS = [
    ("bk_x_000001", "2026-03-31", 275000, PAYER, "inv_000095"),         # the open overage invoice, exactly
    ("bk_x_000002", "2026-03-31", 123457, PAYER, "no such invoice"),    # a known payer, no invoice of that amount
    ("bk_x_000003", "2026-03-31", 275000, "NOBODY HOLDINGS", "unknown"),  # an amount that fits, an unknown payer
]


@pytest.fixture(scope="module")
def mini_extra_receipts(tmp_path_factory) -> Path:
    """The `mini` books plus three bank receipts that have no payment row. Read-only."""

    def make(directory: Path) -> None:
        books = directory / "books"
        shutil.copytree(MINI_BOOKS, books)
        with (books / "bank_transactions.csv").open("a", encoding="utf-8", newline="") as fh:
            for txn, posted, cents, payer, reference in EXTRA_RECEIPTS:
                fh.write(f"{txn},{posted},{cents},{payer},{reference},{posted}\n")
        build(directory / "w.duckdb", books)

    return shared_dir(tmp_path_factory, "ar-mini-extra-receipts", make) / "w.duckdb"


def test_a_receipt_equal_to_exactly_one_open_invoice_of_a_master_customer_is_auto_applied(mini_extra_receipts):
    assert applications(mini_extra_receipts, "bank_txn_id = 'bk_x_000001'") == [
        ("bk_x_000001", None, "inv_000095", "acct_0001", 275000, "auto_applied")]


def test_a_receipt_that_fits_no_invoice_or_comes_from_no_customer_is_unmatched_and_applies_nothing(mini_extra_receipts):
    assert applications(mini_extra_receipts, "bank_txn_id = 'bk_x_000002'") == [
        ("bk_x_000002", None, None, "acct_0001", 0, "unmatched")]
    assert applications(mini_extra_receipts, "bank_txn_id = 'bk_x_000003'") == [
        ("bk_x_000003", None, None, None, 0, "unmatched")]


def test_an_auto_applied_receipt_reduces_receivables_and_an_unmatched_one_does_not(mini_extra_receipts):
    got = {(month, customer): closing for month, customer, *_, closing in rollforward(mini_extra_receipts)}
    expected = {(r["month"], r["customer_id"]): int(r["ar_closing_cents"]) for r in csv_rows(EXPECTED)}
    assert got[("2026-03", "acct_0001")] == expected[("2026-03", "acct_0001")] - 275000
    assert {k: v for k, v in got.items() if k != ("2026-03", "acct_0001")} == {
        k: v for k, v in expected.items() if k != ("2026-03", "acct_0001")}
