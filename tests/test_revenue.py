"""Recognised revenue per customer and month, over the `mini` books.

On the clean books every customer-month equals the independent reference's figure; a line that spans
29 February and a one-day line recognise exactly; the dbt build, which includes the conservation
test on the lines, passes.
"""

from __future__ import annotations

import csv
import shutil
from datetime import date
from pathlib import Path

import pytest

from c2c import db, dbt_runner, load
from c2c.truth_reference import ratable_cents
from conftest import DEFAULT_CONFIG, MINI_BOOKS, MINI_WINDOW, ROOT, shared_dir

REFERENCE = ROOT / "data" / "reference"
EXPECTED = ROOT / "tests" / "fixtures" / "expected" / "rollforward.csv"


def build(books: Path, warehouse: Path) -> None:
    con = db.connect(warehouse)
    try:
        db.init_schemas(con)
        load.load_months(con, books, *MINI_WINDOW, "test")
        load.load_reference(con, REFERENCE)
        load.load_config(con, DEFAULT_CONFIG)
    finally:
        con.close()
    dbt_runner.build(warehouse, select="+fct_revenue")


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
def mini_revenue(tmp_path_factory) -> Path:
    """The `mini` books loaded and revenue built (with its dbt tests). Read-only."""
    return shared_dir(tmp_path_factory, "revenue-mini",
                      lambda directory: build(MINI_BOOKS, directory / "w.duckdb")) / "w.duckdb"


def test_revenue_per_customer_month_equals_the_expected_rollforward(mini_revenue):
    got: dict[tuple[str, str], int] = {}
    for month, customer, cents in query(mini_revenue, "SELECT month, customer_id, sum(recognised_cents)"
                                                      " FROM mart.fct_revenue GROUP BY ALL"):
        got[customer, month] = cents
    expected = {(r["customer_id"], r["month"]): int(r["revenue_cents"]) for r in csv_rows(EXPECTED)}
    assert sum(expected.values()) > 0
    assert {k: v for k, v in got.items() if v} == {k: v for k, v in expected.items() if v}
    assert set(got) <= set(expected)


def test_the_mart_carries_its_columns_and_line_types(mini_revenue):
    columns = [row[0] for row in query(
        mini_revenue, "SELECT column_name FROM information_schema.columns"
                      " WHERE table_schema = 'mart' AND table_name = 'fct_revenue' ORDER BY ordinal_position")]
    assert columns == ["month", "customer_id", "line_type", "recognised_cents"]
    assert {row[0] for row in query(mini_revenue, "SELECT DISTINCT line_type FROM mart.fct_revenue")} \
        == {"subscription", "overage"}


def test_overage_is_recognised_in_its_usage_month(mini_revenue):
    invoices = {}
    contracts = {c["contract_id"]: c["crm_account_id"] for c in csv_rows(MINI_BOOKS / "contracts.csv")}
    for row in csv_rows(MINI_BOOKS / "invoices.csv"):
        if row["line_type"] == "overage":
            key = (row["service_start"][:7], contracts[row["contract_id"]])
            invoices[key] = invoices.get(key, 0) + int(row["amount_cents"])
    got = {(m, c): cents for m, c, cents in query(
        mini_revenue, "SELECT month, customer_id, recognised_cents FROM mart.fct_revenue"
                      " WHERE line_type = 'overage'")}
    assert got == invoices
    assert len(got) > 10


def test_leap_day_and_one_day_lines_recognise_exactly(tmp_path):
    books = tmp_path / "books"
    shutil.copytree(MINI_BOOKS, books)
    path = books / "invoices.csv"
    rows = csv_rows(path)
    template = next(r for r in rows if r["line_type"] == "subscription")
    contracts = {c["contract_id"]: c["crm_account_id"] for c in csv_rows(books / "contracts.csv")}
    customer = contracts[template["contract_id"]]
    lines = [  # (amount, service_start, service_end)
        (1_000_001, date(2024, 2, 15), date(2024, 3, 14)),   # spans 29 February
        (12_345, date(2024, 2, 29), date(2024, 2, 29)),      # the leap day alone
        (-777, date(2024, 3, 7), date(2024, 3, 7)),          # a one-day credit
    ]
    added = []
    for n, (amount, start, end) in enumerate(lines, 1):
        added.append({**template, "invoice_id": f"inv_leap{n}", "line_no": "1", "invoice_date": start.isoformat(),
                      "gl_period": start.isoformat()[:7], "due_date": start.isoformat(),
                      "service_start": start.isoformat(), "service_end": end.isoformat(),
                      "line_type": "credit" if amount < 0 else "subscription", "amount_cents": str(amount),
                      "known_on": start.isoformat()})
    with path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(rows[0]), lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows + added)
    build(books, tmp_path / "w.duckdb")

    got = {(m, t): cents for m, t, cents in query(
        tmp_path / "w.duckdb", "SELECT month, line_type, recognised_cents FROM mart.fct_revenue"
                               " WHERE customer_id = ? AND month < '2024-10' ORDER BY ALL", [customer])}
    expected: dict[tuple[str, str], int] = {}
    for amount, start, end in lines:
        kind = "credit" if amount < 0 else "subscription"
        for month in sorted({f"{start:%Y-%m}", f"{end:%Y-%m}"}):
            expected[month, kind] = expected.get((month, kind), 0) + ratable_cents(amount, start, end, month)
    assert got == expected
    assert sum(got.values()) == sum(amount for amount, _, _ in lines)
    assert got["2024-02", "subscription"] == ratable_cents(1_000_001, *lines[0][1:], "2024-02") + 12_345
