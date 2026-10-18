"""The deferred revenue roll-forward over the `mini` books.

Every customer-month equals the expected roll-forward, each month opens where the one before closed,
and the customers' closings add up to the ledger's deferred revenue balance.
"""

from __future__ import annotations

import csv
from pathlib import Path

import pytest

from c2c import db, dbt_runner, load
from conftest import DEFAULT_CONFIG, MINI_BOOKS, MINI_WINDOW, ROOT, shared_dir

REFERENCE = ROOT / "data" / "reference"
EXPECTED = ROOT / "tests" / "fixtures" / "expected" / "rollforward.csv"


def build(warehouse: Path) -> None:
    con = db.connect(warehouse)
    try:
        db.init_schemas(con)
        load.load_months(con, MINI_BOOKS, *MINI_WINDOW, "test")
        load.load_reference(con, REFERENCE)
        load.load_config(con, DEFAULT_CONFIG)
    finally:
        con.close()
    dbt_runner.build(warehouse, select="+fct_defrev_rollforward")


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
def mini_defrev(tmp_path_factory) -> Path:
    """The `mini` books loaded and the deferred revenue roll-forward built (with its dbt tests). Read-only."""
    return shared_dir(tmp_path_factory, "defrev-mini",
                      lambda directory: build(directory / "w.duckdb")) / "w.duckdb"


def rollforward(warehouse: Path) -> list[tuple]:
    return query(warehouse, "SELECT month, customer_id, opening_cents, additions_cents, reductions_cents,"
                            " closing_cents FROM mart.fct_defrev_rollforward ORDER BY month, customer_id")


def test_the_mart_carries_its_columns(mini_defrev):
    columns = [row[0] for row in query(
        mini_defrev, "SELECT column_name FROM information_schema.columns"
                     " WHERE table_schema = 'mart' AND table_name = 'fct_defrev_rollforward'"
                     " ORDER BY ordinal_position")]
    assert columns == ["month", "customer_id", "opening_cents", "additions_cents", "reductions_cents",
                       "closing_cents"]


def test_every_customer_month_equals_the_expected_rollforward(mini_defrev):
    got = {(customer, month): (opening, additions, reductions, closing)
           for month, customer, opening, additions, reductions, closing in rollforward(mini_defrev)}
    expected = {(r["customer_id"], r["month"]): tuple(int(r[f"defrev_{part}_cents"]) for part in
                                                      ("opening", "additions", "reductions", "closing"))
                for r in csv_rows(EXPECTED)}
    assert sum(closing for *_, closing in expected.values()) > 0
    assert got == expected


def test_each_month_opens_where_the_previous_one_closed(mini_defrev):
    closing: dict[str, int] = {}
    for month, customer, opening, additions, reductions, close in rollforward(mini_defrev):
        assert opening == closing.get(customer, 0), (customer, month)
        assert opening + additions - reductions == close, (customer, month)
        closing[customer] = close


def test_the_customers_closings_add_up_to_the_ledger_deferred_revenue(mini_defrev):
    ledger: dict[str, int] = {}
    for row in csv_rows(MINI_BOOKS / "gl.csv"):
        if row["gl_account"] == "2400":
            ledger[row["gl_period"]] = ledger.get(row["gl_period"], 0) + int(row["amount_cents"])
    totals: dict[str, int] = {}
    for month, *_, closing in rollforward(mini_defrev):
        totals[month] = totals.get(month, 0) + closing
    balance = 0
    for month in sorted(totals):
        balance += ledger.get(month, 0)
        assert totals[month] == -balance, month
