"""The billing schedule of the contracts, over the `mini` books.

On the clean books (no error injected) the schedule ties exactly to the invoices that were issued; an amendment adds a
stub of the right sign; overage is dated the last day of its usage month.
"""

from __future__ import annotations

import calendar
import csv
import shutil
from collections import Counter
from datetime import date, timedelta
from pathlib import Path

import pytest

from c2c import db, dbt_runner, load
from c2c.truth_reference import _cumulative, period_days
from conftest import MINI_BOOKS, MINI_WINDOW, ROOT, shared_dir

REFERENCE = ROOT / "data" / "reference"
KEY = "contract_id, line_id, service_start, service_end, amount_cents"


def build(books: Path, warehouse: Path) -> None:
    con = db.connect(warehouse)
    try:
        db.init_schemas(con)
        load.load_months(con, books, *MINI_WINDOW, "test")
        load.load_reference(con, REFERENCE)
    finally:
        con.close()
    dbt_runner.build(warehouse, select="+fct_billing_schedule")


def query(warehouse: Path, sql: str, params=()):
    con = db.connect(warehouse, read_only=True)
    try:
        return con.execute(sql, params).fetchall()
    finally:
        con.close()


def csv_rows(books: Path, name: str) -> list[dict]:
    with (books / f"{name}.csv").open(encoding="utf-8", newline="") as fh:
        return list(csv.DictReader(fh))


@pytest.fixture(scope="module")
def mini_schedule(tmp_path_factory) -> Path:
    """The `mini` books loaded and the schedule built. Read-only."""
    return shared_dir(tmp_path_factory, "schedule-mini",
                      lambda directory: build(MINI_BOOKS, directory / "w.duckdb")) / "w.duckdb"


def last_day(month: str) -> date:
    year, mon = map(int, month.split("-"))
    return date(year, mon, calendar.monthrange(year, mon)[1])


def issued_keys() -> Counter:
    return Counter((i["contract_id"], i["contract_line_id"], i["service_start"], i["service_end"],
                    int(i["amount_cents"])) for i in csv_rows(MINI_BOOKS, "invoices"))


def test_the_schedule_equals_the_issued_invoices(mini_schedule):
    window_end = last_day(MINI_WINDOW[1]).isoformat()
    rows = query(mini_schedule, f"SELECT {KEY} FROM mart.fct_billing_schedule WHERE invoice_date <= ?", [window_end])
    scheduled = Counter((c, line, s.isoformat(), e.isoformat(), cents) for c, line, s, e, cents in rows)
    issued = issued_keys()
    assert scheduled - issued == Counter(), "scheduled but not issued"
    assert issued - scheduled == Counter(), "issued but not scheduled"
    assert sum(issued.values()) > 50


def test_invoice_dates_follow_the_issued_invoices(mini_schedule):
    issued = Counter((i["contract_line_id"], i["invoice_date"], int(i["amount_cents"]))
                     for i in csv_rows(MINI_BOOKS, "invoices"))
    rows = query(mini_schedule, "SELECT line_id, invoice_date, amount_cents FROM mart.fct_billing_schedule"
                                " WHERE invoice_date <= ?", [last_day(MINI_WINDOW[1]).isoformat()])
    assert Counter((line, d.isoformat(), cents) for line, d, cents in rows) == issued


def test_every_row_is_the_actual_source_with_no_as_of_or_case(mini_schedule):
    assert query(mini_schedule, 'SELECT DISTINCT source, as_of, "case" FROM mart.fct_billing_schedule') == [
        ("actual", None, None)]


def test_the_schedule_carries_the_columns_of_the_mart(mini_schedule):
    columns = [row[0] for row in query(
        mini_schedule, "SELECT column_name FROM information_schema.columns"
                       " WHERE table_schema = 'mart' AND table_name = 'fct_billing_schedule' ORDER BY ordinal_position")]
    assert columns == ["source", "as_of", "case", "customer_id", "contract_id", "line_id", "invoice_date",
                       "service_start", "service_end", "amount_cents"]


def amendment() -> tuple[dict, dict, dict]:
    """The amended contract's first and latest version rows and the fees of its subscription line."""
    contracts = csv_rows(MINI_BOOKS, "contracts")
    amended = {c["contract_id"] for c in contracts if int(c["version"]) > 1}
    assert len(amended) == 1  # the clean books carry one amendment
    contract = amended.pop()
    rows = sorted((c for c in contracts if c["contract_id"] == contract), key=lambda c: int(c["version"]))
    return rows[0], rows[-1], {"contract_id": contract}


def test_the_amendment_customer_has_a_stub_of_the_right_sign(mini_schedule):
    first, latest, ids = amendment()
    fees = {int(ln["version"]): int(ln["annual_fee_cents"]) for ln in csv_rows(MINI_BOOKS, "contract_lines")
            if ln["contract_id"] == ids["contract_id"] and ln["product"] == "subscription"}
    change = fees[int(latest["version"])] - fees[int(first["version"])]
    effective = date.fromisoformat(latest["effective_from"])
    start, end = date.fromisoformat(first["term_start"]), date.fromisoformat(first["term_end"])
    elapsed = _cumulative(change, start, period_days(start, end), effective - timedelta(days=1))
    rows = query(mini_schedule, "SELECT customer_id, invoice_date, service_start, service_end, amount_cents"
                                " FROM mart.fct_billing_schedule WHERE contract_id = ? AND invoice_date = ?"
                                " AND service_start = ? AND amount_cents <> 0 AND line_id LIKE '%_L1'",
                 [ids["contract_id"], effective, effective])
    assert rows == [(first["crm_account_id"], effective, effective, end, change - elapsed)]
    assert change > 0 and rows[0][4] > 0  # a price increase is a charge


def test_a_downgrade_stub_is_a_credit(tmp_path):
    first, latest, ids = amendment()
    books = tmp_path / "books"
    shutil.copytree(MINI_BOOKS, books)
    path = books / "contract_lines.csv"
    lines = csv_rows(books, "contract_lines")
    cheaper = 15_000_000
    for ln in lines:
        if ln["contract_id"] == ids["contract_id"] and ln["version"] == latest["version"] and ln["product"] == "subscription":
            ln["annual_fee_cents"] = str(cheaper)
    with path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(lines[0]), lineterminator="\n")
        writer.writeheader()
        writer.writerows(lines)
    build(books, tmp_path / "w.duckdb")

    before = next(int(ln["annual_fee_cents"]) for ln in csv_rows(MINI_BOOKS, "contract_lines")
                  if ln["contract_id"] == ids["contract_id"] and ln["version"] == first["version"]
                  and ln["product"] == "subscription")
    effective = date.fromisoformat(latest["effective_from"])
    start, end = date.fromisoformat(first["term_start"]), date.fromisoformat(first["term_end"])
    drop = before - cheaper
    expected = -(drop - _cumulative(drop, start, period_days(start, end), effective - timedelta(days=1)))
    rows = query(tmp_path / "w.duckdb", "SELECT service_start, service_end, amount_cents FROM mart.fct_billing_schedule"
                                        " WHERE contract_id = ? AND invoice_date = ? AND line_id LIKE '%_L1'",
                 [ids["contract_id"], effective])
    assert rows == [(effective, end, expected)]
    assert expected < 0


def test_overage_is_dated_the_last_day_of_its_usage_month(mini_schedule):
    rows = query(mini_schedule, "SELECT invoice_date, service_start, service_end, amount_cents"
                                " FROM mart.fct_billing_schedule WHERE line_id LIKE '%_L2'")
    assert len(rows) > 10
    for invoice_date, service_start, service_end, cents in rows:
        assert invoice_date == service_end == last_day(f"{service_end.year}-{service_end.month:02d}")
        assert service_start == service_end.replace(day=1)
        assert cents > 0


def test_overage_is_the_excess_units_at_the_rate(mini_schedule):
    lines = {ln["contract_id"]: ln for ln in csv_rows(MINI_BOOKS, "contract_lines")
             if ln["product"] == "overage" and ln["version"] == "1"}
    expected = Counter()
    for u in csv_rows(MINI_BOOKS, "usage_monthly"):
        ln = lines[u["contract_id"]]
        excess = int(u["units"]) - int(ln["included_units"])
        if excess > 0 and int(ln["overage_cents_per_unit"]):
            expected[(u["contract_id"], u["month"], excess * int(ln["overage_cents_per_unit"]))] += 1
    got = Counter((c, f"{d.year}-{d.month:02d}", cents) for c, d, cents in query(
        mini_schedule, "SELECT contract_id, invoice_date, amount_cents FROM mart.fct_billing_schedule"
                       " WHERE line_id LIKE '%_L2'"))
    # the amended contract's second version raises the allowance; compare only contracts that kept version 1
    unamended = {c for c, _, _ in expected} - {amendment()[2]["contract_id"]}
    assert {k for k in got if k[0] in unamended} == {k for k in expected if k[0] in unamended}
