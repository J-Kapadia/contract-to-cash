"""CTL-CASH-APP and CTL-BANK-UNMATCHED: each passes on the clean `mini` books and fails the close, naming
the payment or the bank row in its remediation, on a copy with the error planted. A receipt that equals
exactly one open invoice of a customer is applied by the books themselves and is no failure."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from c2c import controls, db, dbt_runner, load
from c2c.errors import ControlFailure

from conftest import DEFAULT_CONFIG, MINI_BOOKS, MINI_WINDOW, ROOT, c2c

MONTH = "2026-01"
CASH_APP, UNMATCHED = "CTL-CASH-APP", "CTL-BANK-UNMATCHED"
MISAPPLIED_PAYMENT, PAYER = "pay_000073", "acct_0007"  # the answer key's payment and the customer who paid it
ORPHAN_RECEIPT = "bk_000073"
EXACT_RECEIPT = "bk_x_000001"  # 275000 cents from a known payer: the open overage invoice, exactly


def inject(error: str, out: Path) -> Path:
    done = c2c("inject", "--error", error, "--books", str(MINI_BOOKS), "--out", str(out), "--json")
    assert done.code == 0, done.stderr
    return out


def close(books: Path, warehouse: Path, month: str = MONTH):
    done = c2c("close", "--month", month, "--books", str(books), "--warehouse", str(warehouse), "--json")
    return done, {entry["id"]: entry for entry in done.json.get("failed_controls", [])}


def failed_controls_after_build(books: Path, warehouse: Path) -> dict:
    """Load the books through the month, build the models, run the mart controls and return the failed ones.

    The build leaves out the bridge conservation test: a receipt that applies to no invoice makes the
    bridge's receipts differ from the cash roll-forward's, so a full close of such books stops in dbt
    before any control is read. Here the control is run on the built models directly."""
    con = db.connect(warehouse)
    try:
        db.init_schemas(con)
        load.load_months(con, books, MINI_WINDOW[0], MONTH, "test")
        load.load_reference(con, ROOT / "data" / "reference")
        load.load_config(con, DEFAULT_CONFIG)
    finally:
        con.close()
    dbt_runner.build(warehouse, exclude="tag:forecast assert_bridge_conserves")
    with db.connect(warehouse) as con:
        with pytest.raises(ControlFailure) as raised:
            controls.run(con, MONTH, "mart")
    return {entry["id"]: entry for entry in raised.value.failed_controls}


def test_the_clean_mini_passes_both_controls(mini_books_copy, workspace):
    done = workspace.run("close", "--month", MONTH, "--json")
    assert done.code == 0, done.stderr
    controls = {entry["id"]: entry for entry in done.json["controls"]}
    for control in (CASH_APP, UNMATCHED):
        assert controls[control] == {"id": control, "status": "pass", "violating_rows": 0}


def test_a_payment_re_pointed_to_another_customers_invoice_fails_the_close_naming_it(tmp_path):
    books = inject("misapplied_payment", tmp_path / "copy")

    done, failed = close(books, tmp_path / "w.duckdb")

    assert done.code == 3, done.stderr
    misapplied = failed[CASH_APP]
    assert misapplied["violating_rows"] == 1
    [remediation] = misapplied["remediation"]
    assert MISAPPLIED_PAYMENT in remediation and PAYER in remediation and "bk_" in remediation
    assert "{" not in remediation
    assert len(misapplied["detail"]["row_hashes"]) == 1
    assert UNMATCHED not in failed


def test_a_receipt_from_an_unknown_payer_with_no_payment_row_fails_the_close_naming_the_bank_row(tmp_path):
    books = inject("orphan_bank_txn", tmp_path / "copy")

    failed = failed_controls_after_build(books, tmp_path / "w.duckdb")

    orphan = failed[UNMATCHED]
    assert orphan["violating_rows"] == 1
    [remediation] = orphan["remediation"]
    assert ORPHAN_RECEIPT in remediation and " cents, " in remediation and "{" not in remediation
    assert "payments.csv" in remediation
    assert len(orphan["detail"]["row_hashes"]) == 1
    assert CASH_APP not in failed


def test_a_receipt_equal_to_one_open_invoice_of_a_customer_is_applied_and_passes(tmp_path):
    books = tmp_path / "books"
    shutil.copytree(MINI_BOOKS, books)
    with (books / "bank_transactions.csv").open("a", encoding="utf-8", newline="") as fh:
        fh.write(f"{EXACT_RECEIPT},2026-03-31,275000,Bramble Robotics Limited,inv_000095,2026-03-31\n")
    warehouse = tmp_path / "w.duckdb"

    done, failed = close(books, warehouse, "2026-03")

    assert UNMATCHED not in failed and CASH_APP not in failed, done.stdout
    with db.connect(warehouse, read_only=True) as con:
        assert con.execute("SELECT invoice_id, customer_id, applied_cents, kind FROM int.int_cash_application"
                           " WHERE bank_txn_id = ?", [EXACT_RECEIPT]).fetchall() == [
            ("inv_000095", "acct_0001", 275000, "auto_applied")]
        assert con.execute("SELECT control_id, status FROM ctl.control_results WHERE control_id IN (?, ?)"
                           " AND month = '2026-03' ORDER BY control_id", [CASH_APP, UNMATCHED]).fetchall() == [
            (UNMATCHED, "pass"), (CASH_APP, "pass")]
