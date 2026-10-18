"""The revenue-to-cash bridge over the `mini` books.

Every month equals the expected bridge, each line equals the numbers it is built from, and the
walk conserves: revenue plus the deferred revenue movement is the month's net billings, and net
billings less the receivables movement is the cash receipts.
"""

from __future__ import annotations

import csv
from pathlib import Path

import pytest

from c2c import db, dbt_runner, load
from conftest import MINI_BOOKS, MINI_WINDOW, ROOT, shared_dir

REFERENCE = ROOT / "data" / "reference"
EXPECTED = ROOT / "tests" / "fixtures" / "expected" / "bridge.csv"
LINES = ["revenue", "defrev_movement", "net_billings", "ar_movement", "receipts"]


def build(warehouse: Path) -> None:
    con = db.connect(warehouse)
    try:
        db.init_schemas(con)
        load.load_months(con, MINI_BOOKS, *MINI_WINDOW, "test")
        load.load_reference(con, REFERENCE)
    finally:
        con.close()
    dbt_runner.build(warehouse, select="+fct_bridge")


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
def mini_bridge(tmp_path_factory) -> Path:
    """The `mini` books loaded and the bridge built (with its dbt tests). Read-only."""
    return shared_dir(tmp_path_factory, "bridge-mini",
                      lambda directory: build(directory / "w.duckdb")) / "w.duckdb"


def bridge(warehouse: Path) -> dict[str, dict[str, int]]:
    walk: dict[str, dict[str, int]] = {}
    for month, line, cents in query(warehouse, "SELECT month, line, cents FROM mart.fct_bridge"):
        walk.setdefault(month, {})[line] = cents
    return walk


def sums(warehouse: Path, sql: str) -> dict[str, int]:
    return {month: int(cents) for month, cents in query(warehouse, sql)}


def test_the_mart_carries_its_columns_and_five_lines_a_month(mini_bridge):
    columns = [row[0] for row in query(
        mini_bridge, "SELECT column_name FROM information_schema.columns"
                     " WHERE table_schema = 'mart' AND table_name = 'fct_bridge' ORDER BY ordinal_position")]
    assert columns == ["month", "line", "cents"]
    counts = query(mini_bridge, "SELECT month, count(*), count(DISTINCT line) FROM mart.fct_bridge GROUP BY month")
    assert counts and all(n == distinct == len(LINES) for _, n, distinct in counts)


def test_every_month_equals_the_expected_bridge(mini_bridge):
    expected = {row["month"]: {line: int(row[line]) for line in LINES} for row in csv_rows(EXPECTED)}
    assert expected and any(walk["revenue"] for walk in expected.values())
    assert bridge(mini_bridge) == expected


def test_each_line_equals_its_source_numbers(mini_bridge):
    revenue = sums(mini_bridge, "SELECT month, sum(recognised_cents) FROM mart.fct_revenue GROUP BY month")
    defrev = sums(mini_bridge, "SELECT month, sum(closing_cents - opening_cents)"
                               " FROM mart.fct_defrev_rollforward GROUP BY month")
    ar = sums(mini_bridge, "SELECT month, sum(closing_cents - opening_cents)"
                           " FROM mart.fct_ar_rollforward GROUP BY month")
    for month, walk in bridge(mini_bridge).items():
        assert walk["revenue"] == revenue.get(month, 0), month
        assert walk["defrev_movement"] == defrev.get(month, 0), month
        assert walk["ar_movement"] == ar.get(month, 0), month


def test_net_billings_are_the_signed_sum_of_the_invoice_lines(mini_bridge):
    billed: dict[str, int] = {}
    for row in csv_rows(MINI_BOOKS / "invoices.csv"):
        billed[row["invoice_date"][:7]] = billed.get(row["invoice_date"][:7], 0) + int(row["amount_cents"])
    for month, walk in bridge(mini_bridge).items():
        assert walk["net_billings"] == billed.get(month, 0), month


def test_receipts_equal_the_cash_receipts_and_the_walk_conserves(mini_bridge):
    receipts = sums(mini_bridge, "SELECT month, receipts_cents FROM mart.fct_cash_rollforward")
    for month, walk in bridge(mini_bridge).items():
        assert walk["receipts"] == receipts[month], month
        assert walk["revenue"] + walk["defrev_movement"] == walk["net_billings"], month
        assert walk["net_billings"] - walk["ar_movement"] == walk["receipts"], month
