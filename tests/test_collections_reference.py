"""Collections over the `mini` books against the Python reference.

Aged receivables, days to pay and expected cash of 2026-03 equal the committed expected file row for
row, and the file equals a recomputation from the books. The four edge cases of collecting are asserted
by named tests, on the reference over hand-made rows and on the committed file where `mini` has them.
"""

from __future__ import annotations

import csv
from datetime import date, timedelta
from pathlib import Path

import pytest

from c2c import db, dbt_runner, load
from c2c import truth_reference as tr
from c2c.truth_reference import collections as ref
from conftest import DEFAULT_CONFIG, MINI_BOOKS, MINI_TRUTH, MINI_WINDOW, ROOT, shared_dir

REFERENCE = ROOT / "data" / "reference"
EXPECTED = ROOT / "tests" / "fixtures" / "expected" / "collections_2026-03.csv"
BUCKETS = [0, 30, 60, 90]
AS_OF = date(2026, 3, 31)
CALENDAR = ref.business_days(REFERENCE / "business_days.csv")
COUNT = {"window_months": 24, "min_paid": 3, "at_risk_days": 120}


def csv_rows(path: Path) -> list[dict]:
    with path.open(encoding="utf-8", newline="") as fh:
        return list(csv.DictReader(fh))


def committed() -> list[dict]:
    return csv_rows(EXPECTED)


def tuples(rows: list[dict]) -> list[tuple]:
    return sorted(tuple(row.get(c, "") for c in ref.COLUMNS) for row in rows)


def query(warehouse: Path, sql: str):
    con = db.connect(warehouse, read_only=True)
    try:
        return con.execute(sql).fetchall()
    finally:
        con.close()


def build(warehouse: Path) -> None:
    con = db.connect(warehouse)
    try:
        db.init_schemas(con)
        load.load_months(con, MINI_BOOKS, *MINI_WINDOW, "test")
        load.load_reference(con, REFERENCE)
        load.load_config(con, DEFAULT_CONFIG)
    finally:
        con.close()
    dbt_runner.build(warehouse, select="+fct_ar_aging +fct_days_to_pay +fct_expected_cash")


@pytest.fixture(scope="module")
def mini_warehouse(tmp_path_factory) -> Path:
    """The `mini` books loaded and the three collections marts built (with their dbt tests). Read-only."""
    return shared_dir(tmp_path_factory, "collections-mini", lambda d: build(d / "w.duckdb")) / "w.duckdb"


def warehouse_rows(warehouse: Path) -> list[dict]:
    month = MINI_WINDOW[1]
    rows = [{"table": "aging", "customer_id": c, "bucket": b, "open_cents": str(n)} for c, b, n in query(
        warehouse, f"SELECT customer_id, bucket, open_cents FROM mart.fct_ar_aging WHERE month = '{month}'")]
    rows += [{"table": "dtp", "customer_id": c, "paid_invoices": str(n), "median_days": str(m), "p80_days": str(p),
              "fallback": str(f).lower()} for c, n, m, p, f in query(
        warehouse, "SELECT customer_id, paid_invoices, median_days, p80_days, fallback"
                   f" FROM mart.fct_days_to_pay WHERE month = '{month}'")]
    rows += [{"table": "expected_cash", "customer_id": c, "invoice_id": i, "case": k, "expected_date": d.isoformat(),
              "cents": str(n), "at_risk": str(r).lower()} for i, c, k, d, n, r in query(
        warehouse, 'SELECT invoice_id, customer_id, "case", expected_date, cents, at_risk'
                   f" FROM mart.fct_expected_cash WHERE month = '{month}'")]
    return rows


# ---------------------------------------------------------------- the marts against the file

def test_aging_days_to_pay_and_expected_cash_equal_the_expected_file_row_for_row(mini_warehouse):
    expected = committed()
    assert {row["table"] for row in expected} == {"aging", "dtp", "expected_cash"}
    assert tuples(warehouse_rows(mini_warehouse)) == tuples(expected)


def test_the_expected_file_equals_a_recomputation_from_the_books():
    files = ref.expected(MINI_BOOKS, MINI_TRUTH, ROOT / "tests" / "fixtures" / "config")
    assert files == {EXPECTED.name: EXPECTED.read_text(encoding="utf-8")}


def test_the_reference_dates_every_open_invoice_upside_no_later_than_base_and_base_no_later_than_downside():
    customer_of = {r["source_id"]: r["true_customer_id"] for r in csv_rows(MINI_TRUTH / "identities.csv")
                   if r["source_system"] == "billing"}
    invoices = [{**r, "customer_id": customer_of[r["billing_customer_id"]]} for r in csv_rows(MINI_BOOKS / "invoices.csv")]
    owner = {r["invoice_id"]: r["customer_id"] for r in invoices}
    payments = [{**r, "customer_id": owner.get(r["invoice_id"], customer_of[r["billing_customer_id"]])}
                for r in csv_rows(MINI_BOOKS / "payments.csv")]
    checked = 0
    for month in tr.months_between(*MINI_WINDOW):
        by_invoice: dict[str, dict[str, date]] = {}
        for row in ref.expected_cash(invoices, payments, tr.month_end(month), calendar=CALENDAR, **COUNT):
            by_invoice.setdefault(row["invoice_id"], {})[row["case"]] = row["expected_date"]
        for invoice_id, on in by_invoice.items():
            assert on["upside"] <= on["base"] <= on["downside"], (month, invoice_id, on)
            checked += 1
    assert checked > 10


# ---------------------------------------------------------------- the edge cases


def invoice(invoice_id: str, customer: str, issued: date, due: date, cents: int) -> dict:
    return {"invoice_id": invoice_id, "customer_id": customer, "invoice_date": issued, "due_date": due,
            "amount_cents": cents}


def payment(invoice_id: str | None, customer: str, on: date, cents: int) -> dict:
    return {"invoice_id": invoice_id, "customer_id": customer, "applied_on": on, "applied_cents": cents}


def test_ec09_partial_over_and_shared_payments_date_an_invoice_by_the_payment_that_covers_it():
    long_ago = date(2025, 12, 1)
    invoices = [invoice("A", "c1", long_ago, long_ago + timedelta(days=30), 1000),
                invoice("B", "c1", long_ago, long_ago + timedelta(days=30), 1000),
                invoice("C", "c1", long_ago, long_ago + timedelta(days=30), 600),
                invoice("D", "c1", long_ago, long_ago + timedelta(days=30), 400),
                invoice("E", "c1", long_ago, long_ago + timedelta(days=30), 1000)]
    payments = [payment("A", "c1", date(2025, 12, 20), 400), payment("A", "c1", date(2026, 1, 15), 600),  # instalments
                payment("B", "c1", date(2025, 12, 10), 1500),                                           # overpaid
                payment("C", "c1", date(2025, 12, 5), 600), payment("D", "c1", date(2025, 12, 5), 400),  # one receipt
                payment("E", "c1", date(2025, 12, 25), 300)]                                           # still partly open
    paid = ref.paid_invoices(invoices, payments)
    assert {k: v["paid_on"] for k, v in paid.items()} == {
        "A": date(2026, 1, 15), "B": date(2025, 12, 10), "C": date(2025, 12, 5), "D": date(2025, 12, 5)}
    stats = ref.days_to_pay(invoices, payments, AS_OF, 24, 3)
    # days to pay 45, 9, 4, 4: the instalment invoice is dated by its last payment
    assert stats["c1"] == (4, 4, 45, False) and stats["*"] == (4, 4, 45, False)
    aged = ref.aging(invoices, payments, AS_OF, BUCKETS)
    assert aged["c1", "61-90"] == 700 - 500           # E owes 700 and B has 500 of credit, all due 90 days before
    assert sum(aged.values()) == aged["c1", "61-90"]
    # the mini books carry an invoice paid in instalments: its date is the last application
    with (MINI_BOOKS / "payments.csv").open(encoding="utf-8", newline="") as fh:
        books = [{**r, "customer_id": r["billing_customer_id"], "applied_cents": int(r["applied_cents"])}
                 for r in csv.DictReader(fh)]
    with (MINI_BOOKS / "invoices.csv").open(encoding="utf-8", newline="") as fh:
        lines = [{**r, "customer_id": r["billing_customer_id"]} for r in csv.DictReader(fh)]
    assert sum(1 for r in books if r["invoice_id"] == "inv_000011") > 1
    assert ref.paid_invoices(lines, books)["inv_000011"]["paid_on"] == date(2025, 5, 14)


def test_ec13_an_expected_date_on_a_weekend_moves_to_the_next_business_day():
    saturday = date(2026, 4, 4)
    assert saturday.weekday() == 5
    row = invoice("W", "c1", saturday - timedelta(days=20), saturday + timedelta(days=10), 1)
    assert ref.expected_pay_date(row, AS_OF, 20, 0, CALENDAR) == date(2026, 4, 6)
    for entry in committed():
        if entry["table"] == "expected_cash":
            on = date.fromisoformat(entry["expected_date"])
            assert on in CALENDAR and on > AS_OF, entry


def test_ec14_a_customer_with_fewer_than_three_paid_invoices_takes_the_pooled_values():
    issued = date(2026, 1, 2)
    invoices, payments = [], []
    for n, (customer, days) in enumerate([("few", 10), ("few", 20), ("many", 30), ("many", 40), ("many", 50), ("many", 60)]):
        invoices.append(invoice(f"I{n}", customer, issued, issued + timedelta(days=30), 100))
        payments.append(payment(f"I{n}", customer, issued + timedelta(days=days), 100))
    stats = ref.days_to_pay(invoices, payments, AS_OF, 24, 3)
    pooled = stats["*"]
    assert pooled == (6, 30, 50, False)
    assert stats["few"] == (2, 30, 50, True)
    assert stats["many"] == (4, 40, 60, False)
    assert not any(row["fallback"] == "true" for row in committed() if row["table"] == "dtp" and row["customer_id"] == "*")


def test_ec15_an_invoice_more_than_120_days_past_due_is_at_risk_and_brings_no_base_or_upside_cash():
    due = AS_OF - timedelta(days=121)
    invoices = [invoice("LATE", "c1", due - timedelta(days=30), due, 5000),
                invoice("EDGE", "c1", due - timedelta(days=30), due + timedelta(days=1), 7000)]
    rows = {(r["invoice_id"], r["case"]): r for r in ref.expected_cash(invoices, [], AS_OF, calendar=CALENDAR, **COUNT)}
    assert [rows["LATE", c]["cents"] for c in ref.CASES] == [0, 5000, 0]
    assert all(rows["LATE", c]["at_risk"] for c in ref.CASES)
    assert [rows["EDGE", c]["cents"] for c in ref.CASES] == [7000, 7000, 7000]
    assert not any(rows["EDGE", c]["at_risk"] for c in ref.CASES)
    for entry in committed():
        if entry["table"] == "expected_cash" and entry["at_risk"] == "true" and entry["case"] != "downside":
            assert entry["cents"] == "0", entry
