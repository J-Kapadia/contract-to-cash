"""The cash roll-forward over the `mini` books.

Every month's closing equals the general ledger's cash balance and the bank's running balance, the
first month opens at the bank's opening-balance row, and each month opens where the one before closed.
"""

from __future__ import annotations

import csv
from pathlib import Path

import pytest

from c2c import db, dbt_runner, load
from conftest import MINI_BOOKS, MINI_WINDOW, ROOT, shared_dir

REFERENCE = ROOT / "data" / "reference"


def build(warehouse: Path) -> None:
    con = db.connect(warehouse)
    try:
        db.init_schemas(con)
        load.load_months(con, MINI_BOOKS, *MINI_WINDOW, "test")
        load.load_reference(con, REFERENCE)
    finally:
        con.close()
    dbt_runner.build(warehouse, select="+fct_cash_rollforward")


def query(warehouse: Path, sql: str, params=()):
    con = db.connect(warehouse, read_only=True)
    try:
        return con.execute(sql, params).fetchall()
    finally:
        con.close()


def csv_rows(name: str) -> list[dict]:
    with (MINI_BOOKS / name).open(encoding="utf-8", newline="") as fh:
        return list(csv.DictReader(fh))


@pytest.fixture(scope="module")
def mini_cash(tmp_path_factory) -> Path:
    """The `mini` books loaded and the cash roll-forward built (with its dbt tests). Read-only."""
    return shared_dir(tmp_path_factory, "cash-mini",
                      lambda directory: build(directory / "w.duckdb")) / "w.duckdb"


def rollforward(warehouse: Path) -> list[tuple]:
    return query(warehouse, "SELECT month, opening_cents, receipts_cents, disbursements_cents, closing_cents"
                            " FROM mart.fct_cash_rollforward ORDER BY month")


def test_the_mart_carries_its_columns_and_every_month_of_the_window(mini_cash):
    columns = [row[0] for row in query(
        mini_cash, "SELECT column_name FROM information_schema.columns"
                   " WHERE table_schema = 'mart' AND table_name = 'fct_cash_rollforward' ORDER BY ordinal_position")]
    assert columns == ["month", "opening_cents", "receipts_cents", "disbursements_cents", "closing_cents"]
    months = [row[0] for row in rollforward(mini_cash)]
    assert months[0] == MINI_WINDOW[0] and months[-1] == MINI_WINDOW[1]
    assert len(months) == len(set(months)) == 18


def test_every_month_closes_at_the_ledger_cash_balance(mini_cash):
    ledger: dict[str, int] = {}
    for row in csv_rows("gl.csv"):
        if row["gl_account"] == "1000":
            ledger[row["gl_period"]] = ledger.get(row["gl_period"], 0) + int(row["amount_cents"])
    balance, expected = 0, {}
    for month, *_ in rollforward(mini_cash):
        balance += ledger.get(month, 0)
        expected[month] = balance
    assert {m: closing for m, *_, closing in rollforward(mini_cash)} == expected


def test_every_month_closes_at_the_bank_running_balance(mini_cash):
    balance, expected = 0, {}
    for row in sorted(csv_rows("bank_transactions.csv"), key=lambda r: (r["posted_on"], r["bank_txn_id"])):
        balance += int(row["amount_cents"])
        expected[row["posted_on"][:7]] = balance
    got = {m: closing for m, *_, closing in rollforward(mini_cash)}
    last = None
    for month in got:  # a month without a bank row closes where the last one did
        last = expected.get(month, last)
        assert got[month] == last, month


def test_the_first_month_opens_at_the_opening_row_and_months_chain(mini_cash):
    rows = rollforward(mini_cash)
    opening_row = csv_rows("bank_transactions.csv")[0]
    assert opening_row["counterparty_text"] == "OPENING BALANCE"
    assert rows[0][1] == int(opening_row["amount_cents"]) > 0
    for (_, _, _, _, closing), (month, opening, *_) in zip(rows, rows[1:]):
        assert opening == closing, month


def test_receipts_are_positive_amounts_and_disbursements_negative_ones(mini_cash):
    rows = rollforward(mini_cash)
    for month, opening, receipts, disbursements, closing in rows:
        assert opening + receipts - disbursements == closing
        assert receipts >= 0 and disbursements >= 0
    bank = [r for r in csv_rows("bank_transactions.csv") if r["counterparty_text"] != "OPENING BALANCE"]
    assert sum(r[2] for r in rows) == sum(int(r["amount_cents"]) for r in bank if int(r["amount_cents"]) > 0)
    assert sum(r[3] for r in rows) == -sum(int(r["amount_cents"]) for r in bank if int(r["amount_cents"]) < 0)
    assert sum(r[3] for r in rows) > 0


def test_the_staged_opening_flag_marks_only_the_first_row(mini_cash):
    flagged = query(mini_cash, "SELECT bank_txn_id, amount_cents FROM int.stg_bank_transactions"
                               " WHERE is_opening_balance")
    assert flagged == [(csv_rows("bank_transactions.csv")[0]["bank_txn_id"],
                        int(csv_rows("bank_transactions.csv")[0]["amount_cents"]))]
