"""Each injected error is caught by the control assigned to it, alone and in the combined copy.

Every copy is closed in its own temporary warehouse. A single-error copy is closed through the month the
answer key plants it in; the combined copy holds the errors in seven different months, and is closed the
way a person works through it: the errors already dealt with are left out of the copy, so the close runs
on to the next one, passes through the months before it and fails at it.
"""

from __future__ import annotations

import csv
from datetime import timedelta
from pathlib import Path

import pytest

from c2c import controls, db, dbt_runner
from c2c.errors import ControlFailure
from c2c.gen.plan import INJECT_ERRORS
from c2c.inject import PLANTED_ORDER, inject_all, plan_all
from c2c.truth_reference import month_of, month_start

from conftest import MINI_BOOKS, MINI_TRUTH, MINI_WINDOW, c2c

pytestmark = pytest.mark.slow

MONTH = "2026-01"
SEED = 1
NAMED = {
    "duplicate_customer": "CTL-MASTER-REVIEW",
    "wrong_period": "CTL-CUTOFF",
    "misapplied_payment": "CTL-CASH-APP",
    "unbilled_amendment": "CTL-BILL-CONTRACT",
    "defrev_gl_slip": "CTL-TIE-DEFREV",
    "orphan_bank_txn": "CTL-BANK-UNMATCHED",
    "rounding_slip": "CTL-TIE-AR",
}
# The row key the control's remediation must name: the file of the changed row and its column.
KEY_COLUMN = {
    "duplicate_customer": ("billing_customers.csv", "billing_customer_id"),
    "misapplied_payment": ("payments.csv", "payment_id"),
    "unbilled_amendment": ("contracts.csv", "contract_id"),
    "defrev_gl_slip": ("gl.csv", "entry_id"),
    "orphan_bank_txn": ("bank_transactions.csv", "bank_txn_id"),
    "rounding_slip": ("gl.csv", "entry_id"),
}
# Controls that report no ledger or books rows by hash: they name the customers, contracts or pairs instead.
NO_ROW_HASHES = {"CTL-MASTER-REVIEW", "CTL-BILL-CONTRACT"}
# A control that fails alongside the named one, because the planted row also moves what it checks.
ALSO_FAILS = {
    "duplicate_customer": {"CTL-MASTER-ANCHOR"},
    "wrong_period": {"CTL-TIE-AR", "CTL-TIE-DEFREV", "CTL-TIE-REV"},
    "defrev_gl_slip": {"CTL-TIE-REV"},
    "orphan_bank_txn": {"CTL-TIE-AR"},
}
assert set(NAMED) == set(INJECT_ERRORS) == set(PLANTED_ORDER)


def rows(path: Path) -> list[dict]:
    with path.open(encoding="utf-8", newline="") as fh:
        return list(csv.DictReader(fh))


def inject(error: str, out: Path) -> dict:
    done = c2c("inject", "--error", error, "--books", str(MINI_BOOKS), "--out", str(out), "--json")
    assert done.code == 0, done.stderr
    return done.json


def close(books: Path, warehouse: Path, month: str):
    """Close through `month`; returns the exit code and the failed controls by id.

    A receipt that applies to no invoice makes the bridge test stop dbt before any control is read, so for
    that one the models are built without the bridge test and the mart controls are run on them directly."""
    done = c2c("close", "--month", month, "--books", str(books), "--warehouse", str(warehouse), "--json")
    if done.code == 70 and "assert_bridge_conserves" in done.json["message"]:
        dbt_runner.build(warehouse, exclude="tag:forecast assert_bridge_conserves")
        with db.connect(warehouse) as con:
            with pytest.raises(ControlFailure) as raised:
                controls.run(con, month, "mart")
        return 3, {entry["id"]: entry for entry in raised.value.failed_controls}
    return done.code, {entry["id"]: entry for entry in done.json.get("failed_controls", [])}


def named_key(error: str, copy: Path, changed: list[dict]) -> str:
    """The key of a row the error changed, as the control's remediation is to name it."""
    if error == "wrong_period":
        entry = changed[0]["key"]
        return next(r["source_ref"] for r in rows(copy / "gl.csv")
                    if (r["entry_id"], r["line_no"]) == (entry["entry_id"], entry["line_no"]))
    file, column = KEY_COLUMN[error]
    return next(c["key"][column] for c in changed if c["file"] == file)


def assert_caught(error: str, failed: dict, copy: Path, changed: list[dict]) -> None:
    control = NAMED[error]
    assert control in failed, f"{error}: failed {sorted(failed)}"
    assert set(failed) - {control} <= ALSO_FAILS.get(error, set()), f"{error}: failed {sorted(failed)}"
    entry = failed[control]
    assert entry["violating_rows"] >= 1
    assert entry["remediation"] and all(text.strip() and "{" not in text for text in entry["remediation"])
    assert any(named_key(error, copy, changed) in text for text in entry["remediation"]), entry["remediation"]
    if control not in NO_ROW_HASHES:
        assert entry["detail"]["row_hashes"], f"{error}: {control} names no row hash"


@pytest.mark.parametrize("error", INJECT_ERRORS)
def test_each_error_fails_the_close_with_its_named_control(error, tmp_path):
    envelope = inject(error, tmp_path / "copy")

    code, failed = close(tmp_path / "copy", tmp_path / "w.duckdb", MONTH)

    assert code == 3
    assert_caught(error, failed, tmp_path / "copy", envelope["changed"])


def test_the_clean_copy_passes(tmp_path):
    code, failed = close(MINI_BOOKS, tmp_path / "w.duckdb", MONTH)
    assert (code, failed) == (0, {})


# ------------------------------------------------------------------ the combined copy


def months_before(month: str, count: int) -> str:
    for _ in range(count):
        month = month_of(month_start(month) - timedelta(days=1))
    return month


@pytest.mark.parametrize("position", range(len(PLANTED_ORDER)))
def test_the_combined_copy_passes_before_each_planted_month_and_fails_at_it(position, tmp_path):
    months = plan_all(MINI_TRUTH)
    assert len(set(months.values())) == len(INJECT_ERRORS) and max(months.values()) == MONTH
    error = PLANTED_ORDER[position]
    remaining = PLANTED_ORDER[position:]  # the errors before this one have been corrected in the books
    planted = inject_all(MINI_BOOKS, MINI_TRUTH, tmp_path / "copy", SEED, remaining)
    changed = next(p.changed for p in planted if p.error == error)

    first = months_before(months[error], 2)
    assert first >= MINI_WINDOW[0]
    code, failed = close(tmp_path / "copy", tmp_path / "w.duckdb", first)
    assert (code, failed) == (0, {}), f"{first} is before every remaining error"

    code, failed = close(tmp_path / "copy", tmp_path / "w.duckdb", months[error])
    assert code == 3
    assert_caught(error, failed, tmp_path / "copy", changed)


def test_the_combined_copy_holds_all_seven_errors_in_seven_months(tmp_path):
    done = c2c("inject", "--all", "--books", str(MINI_BOOKS), "--out", str(tmp_path / "copy"), "--json")
    assert done.code == 0, done.stderr
    errors = done.json["errors"]
    assert sorted(e["error"] for e in errors) == sorted(INJECT_ERRORS)
    assert len({e["month"] for e in errors}) == len(INJECT_ERRORS)
    assert {e["error"]: e["month"] for e in errors} == plan_all(MINI_TRUTH)
    assert all(e["rows"] for e in errors)
    assert (tmp_path / "copy" / "books.manifest.sha256").is_file()
