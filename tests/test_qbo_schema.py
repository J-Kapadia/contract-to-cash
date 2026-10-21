"""CTL-DRIFT-SCHEMA at intake: a QuickBooks Online export whose columns changed since the latest earlier
intake stops the intake, and each header is recorded as a number that `c2c explain` resolves."""

from __future__ import annotations

import pytest

from c2c import db, errors
from c2c.intake import qbo_schema

from conftest import c2c

GENERAL_LEDGER = ["Date", "Transaction Type", "Num", "Name", "Memo/Description", "Account", "Split", "Amount", "Balance"]


@pytest.fixture
def warehouse(tmp_path):
    return tmp_path / "w.duckdb"


def test_with_no_record_the_check_passes(warehouse):
    with db.connect(warehouse) as con:
        result = qbo_schema.check(con, "2026-03", "general_ledger", GENERAL_LEDGER)
    assert (result.id, result.status, result.violating_rows) == ("CTL-DRIFT-SCHEMA", "pass", 0)


def test_the_same_columns_pass_and_a_dropped_column_fails_with_one_row(warehouse):
    without_split = [c for c in GENERAL_LEDGER if c != "Split"]
    with db.connect(warehouse) as con:
        qbo_schema.record(con, "2026-02", "general_ledger", GENERAL_LEDGER)
        assert qbo_schema.check(con, "2026-03", "general_ledger", GENERAL_LEDGER).status == "pass"
        assert qbo_schema.check(con, "2026-03", "invoice_list", without_split).status == "pass"
        result = qbo_schema.check(con, "2026-03", "general_ledger", without_split)
    assert (result.status, result.violating_rows) == ("fail", 1)
    assert result.detail == {"row_hashes": [], "number_ids": ["qbo_schema/2026-02/general_ledger"]}
    (text,) = result.remediation
    for part in ("general_ledger", "2026-02", ",".join(without_split), ",".join(GENERAL_LEDGER)):
        assert part in text
    assert "{" not in text


def test_the_baseline_is_strictly_earlier_and_the_latest(warehouse):
    renamed = ["Posting Date", *GENERAL_LEDGER[1:]]
    with db.connect(warehouse) as con:
        qbo_schema.record(con, "2026-01", "general_ledger", GENERAL_LEDGER)
        qbo_schema.record(con, "2026-02", "general_ledger", renamed)
        assert qbo_schema.baseline(con, "2026-02", "general_ledger") == ("2026-01", GENERAL_LEDGER)
        assert qbo_schema.baseline(con, "2026-03", "general_ledger") == ("2026-02", renamed)
        assert qbo_schema.baseline(con, "2026-01", "general_ledger") is None
        assert qbo_schema.baseline(con, "2026-03", "invoice_list") is None


def test_baseline_of_the_month_itself_is_none(warehouse):
    with db.connect(warehouse) as con:
        qbo_schema.record(con, "2026-02", "general_ledger", GENERAL_LEDGER)
        assert qbo_schema.baseline(con, "2026-02", "general_ledger") is None


def test_failure_raises_with_the_failed_control_and_passes_through_otherwise(warehouse):
    with db.connect(warehouse) as con:
        qbo_schema.record(con, "2026-02", "general_ledger", GENERAL_LEDGER)
        ok = qbo_schema.check(con, "2026-03", "general_ledger", GENERAL_LEDGER)
        bad = qbo_schema.check(con, "2026-03", "general_ledger", GENERAL_LEDGER[:-1])
    qbo_schema.failure([ok])
    with pytest.raises(errors.ControlFailure) as raised:
        qbo_schema.failure([ok, bad], "2026-03")
    assert str(raised.value) == "2026-03 intake stopped: CTL-DRIFT-SCHEMA failed."
    assert [c["id"] for c in raised.value.failed_controls] == ["CTL-DRIFT-SCHEMA"]


def test_a_record_resolves_through_explain(warehouse):
    with db.connect(warehouse) as con:
        # explain searches the policy table for a leaf's source rows; a loaded warehouse always has it
        con.execute("CREATE SCHEMA raw")
        con.execute("CREATE TABLE raw.ref_policy (key VARCHAR, value BIGINT)")
        qbo_schema.record(con, "2026-02", "general_ledger", GENERAL_LEDGER)
    done = c2c("explain", "qbo_schema/2026-02/general_ledger", "--warehouse", str(warehouse))
    assert done.code == 0, done.stderr
    assert ",".join(GENERAL_LEDGER) in done.stdout


def test_recording_again_replaces_the_layout(warehouse):
    with db.connect(warehouse) as con:
        qbo_schema.record(con, "2026-02", "general_ledger", GENERAL_LEDGER)
        qbo_schema.record(con, "2026-02", "general_ledger", GENERAL_LEDGER[:-1])
        assert qbo_schema.baseline(con, "2026-03", "general_ledger") == ("2026-02", GENERAL_LEDGER[:-1])
