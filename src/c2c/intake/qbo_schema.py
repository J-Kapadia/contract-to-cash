"""The layout of each QuickBooks Online export, recorded at intake and compared with the latest earlier one.

A parser that keys on column names would read nothing, or the wrong thing, once an export gains, loses
or renames a column. Each intake records the header of every report as a number of its own,
`qbo_schema/<month>/<report>`, and CTL-DRIFT-SCHEMA fails when a header differs from the one recorded at
the latest earlier intake. The family prefix `qbo_schema` is not a family file, so the close never
rewrites these records, and the close-time schema control, which joins its `schema/` records with the
raw tables, never sees them.

Choice made where the brief is silent: the header is stored as the columns joined with a comma, so a
column name that itself holds a comma is not told apart from two columns. `failure` takes the month
as an optional argument because the results alone do not carry it.
"""

from __future__ import annotations

from collections.abc import Sequence

import duckdb

from .. import controls, errors, prov

REPORTS = ("general_ledger", "invoice_list", "customer_balance_detail")
CONTROL_ID = "CTL-DRIFT-SCHEMA"
PREFIX = "qbo_schema"
REMEDIATION = ("The columns of {report} differ from the {baseline_month} intake: now {columns}, then "
               "{previous_columns}; restore the export's layout, or run the intake for {baseline_month} again "
               "on the new layout so it becomes the baseline.")


def _number_id(month: str, report: str) -> str:
    return f"{PREFIX}/{month}/{report}"


def record(con: duckdb.DuckDBPyConnection, month: str, report: str, columns: Sequence[str]) -> None:
    """Write the layout of `report` as exported for `month` (a constant over the layout, like a policy leaf)."""
    prov.init(con)
    joined = ",".join(columns)
    constant = f"{report}:{joined}".replace("'", "''")
    prov.write_leaf(con, _number_id(month, report), month, joined, "text", "intake",
                    f"select sha256('{constant}') as _row_hash")


def baseline(con: duckdb.DuckDBPyConnection, month: str, report: str) -> tuple[str, list[str]] | None:
    """The month and columns of the latest record of `report` strictly before `month`, or None."""
    prov.init(con)
    row = con.execute(
        "SELECT month, value_text FROM prov.records WHERE starts_with(number_id, ?) AND ends_with(number_id, ?)"
        " AND month < ? ORDER BY month DESC LIMIT 1",
        [f"{PREFIX}/", f"/{report}", month]).fetchone()
    if row is None:
        return None
    return row[0], row[1].split(",") if row[1] else []


def check(con: duckdb.DuckDBPyConnection, month: str, report: str, columns: Sequence[str]) -> controls.ControlResult:
    """CTL-DRIFT-SCHEMA for one report: pass with no earlier record or an equal header, else fail."""
    previous = baseline(con, month, report)
    if previous is None or previous[1] == list(columns):
        return controls.ControlResult(id=CONTROL_ID, status="pass", violating_rows=0)
    baseline_month, previous_columns = previous
    remediation = REMEDIATION.format(report=report, baseline_month=baseline_month, columns=",".join(columns),
                                     previous_columns=",".join(previous_columns))
    return controls.ControlResult(id=CONTROL_ID, status="fail", violating_rows=1,
                                  detail={"row_hashes": [], "number_ids": [_number_id(baseline_month, report)]},
                                  remediation=[remediation])


def failure(results: Sequence[controls.ControlResult], month: str = "") -> None:
    """Raise ControlFailure when any of `results` failed; `month` names the intake in the message."""
    failed = [r for r in results if r.failed]
    if failed:
        subject = f"{month} intake" if month else "intake"
        raise errors.ControlFailure(f"{subject} stopped: {CONTROL_ID} failed.", [r.failure() for r in failed])
