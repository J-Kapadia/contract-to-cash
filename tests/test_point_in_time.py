"""A forecast as of A reads only rows that entered the source system on or before A.

A row seeded into the closed `mini` warehouse with a later `known_on` is in none of the
point-in-time views; the same row with an earlier `known_on` is in all of them; the scratch
schema is dropped when a build starts.
"""

from __future__ import annotations

from datetime import date

import pytest

from c2c import db, policy
from conftest import DEFAULT_CONFIG
from forecast_helpers import forecast_built, mini_closed, mini_closed_copy  # noqa: F401  (fixtures)

AS_OF = date(2025, 10, 31)
LATER, EARLIER = "2025-11-15", "2025-10-15"
TABLES = {
    "pit_contracts": "contracts", "pit_contract_lines": "contract_lines", "pit_invoices": "invoices",
    "pit_payments": "payments", "pit_bank": "bank_transactions", "pit_pipeline": "pipeline_snapshots",
    "pit_usage": "usage_monthly", "pit_payroll": "payroll_runs", "pit_headcount_plan": "headcount_plan",
    "pit_commission_plan": "commission_plan", "pit_ap_bills": "ap_bills",
    "pit_vendor_commitments": "vendor_commitments",
}


def seed(warehouse, marker: str, known_on: str) -> None:
    """Copy one row of every book into `raw` with `_row_hash` set to `marker` and the given `known_on`."""
    con = db.connect(warehouse)
    try:
        for table in TABLES.values():
            con.execute(f"INSERT INTO raw.{table} SELECT * REPLACE ('{marker}' AS _row_hash, "
                        f"'{known_on}' AS known_on) FROM raw.{table} LIMIT 1")
    finally:
        con.close()


def marked(warehouse, view: str, marker: str) -> int:
    con = db.connect(warehouse, read_only=True)
    try:
        return con.execute(f"SELECT count(*) FROM fc_build.{view} WHERE _row_hash = ?", [marker]).fetchone()[0]
    finally:
        con.close()


def staged_inputs(warehouse) -> list[tuple]:
    con = db.connect(warehouse, read_only=True)
    try:
        return con.execute("SELECT * FROM fc_build.inputs ORDER BY \"case\", driver").fetchall()
    finally:
        con.close()


def test_a_row_known_after_the_as_of_date_is_in_no_view_and_an_earlier_one_is_in_all(mini_closed_copy):
    seed(mini_closed_copy, "seeded-later", LATER)
    seed(mini_closed_copy, "seeded-earlier", EARLIER)
    forecast_built(mini_closed_copy, AS_OF)

    for view in TABLES:
        assert marked(mini_closed_copy, view, "seeded-later") == 0, view
        assert marked(mini_closed_copy, view, "seeded-earlier") == 1, view


def test_the_staged_inputs_are_the_policys_cases_for_the_as_of_date(mini_closed_copy):
    seed(mini_closed_copy, "seeded-later", LATER)
    forecast_built(mini_closed_copy, AS_OF)

    cases = policy.load(DEFAULT_CONFIG).cases
    rows = staged_inputs(mini_closed_copy)
    assert {r[0] for r in rows} == {AS_OF}
    assert {r[1] for r in rows} == {"base", "downside", "upside"}
    assert {(r[1], r[2]): r[3] for r in rows}[("downside", "pipeline_shift_days")] == cases.downside.pipeline_shift_days
    drivers = [r for r in rows if not r[2].startswith("rate_")]
    assert len(drivers) == 3 * 9
    assert not any(r[4] for r in drivers)
    assert len(rows) == 3 * 9 + 3 * 9  # plus each of the nine measured rates under each case


def test_fc_build_is_dropped_when_a_build_starts(mini_closed_copy):
    con = db.connect(mini_closed_copy)
    try:
        con.execute("CREATE SCHEMA fc_build")
        con.execute("CREATE TABLE fc_build.leftover AS SELECT 1 AS one")
    finally:
        con.close()

    forecast_built(mini_closed_copy, AS_OF)

    con = db.connect(mini_closed_copy, read_only=True)
    try:
        left = con.execute("SELECT count(*) FROM information_schema.tables "
                           "WHERE table_schema = 'fc_build' AND table_name = 'leftover'").fetchone()[0]
    finally:
        con.close()
    assert left == 0
