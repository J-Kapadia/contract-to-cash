"""Days to pay over the `mini` books.

Every customer-month equals the lower median and nearest-rank 80th percentile computed here from the
books, a customer with too few paid invoices takes the pooled values and says so, an invoice paid in
instalments is dated by its last one, and the provenance family records the days and the fallback.
"""

from __future__ import annotations

import csv
from collections import defaultdict
from datetime import date
from pathlib import Path

import pytest

from c2c import db, dbt_runner, load, policy, prov
from c2c import truth_reference as tr
from conftest import DEFAULT_CONFIG, MINI_BOOKS, MINI_WINDOW, ROOT, shared_dir

REFERENCE = ROOT / "data" / "reference"
POOLED = "*"
POLICY = policy.load(DEFAULT_CONFIG)


def build(warehouse: Path, books: Path = MINI_BOOKS) -> None:
    con = db.connect(warehouse)
    try:
        db.init_schemas(con)
        load.load_months(con, books, *MINI_WINDOW, "test")
        load.load_reference(con, REFERENCE)
        load.load_config(con, DEFAULT_CONFIG)
    finally:
        con.close()
    dbt_runner.build(warehouse, select="+fct_days_to_pay")


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
def mini_dtp(tmp_path_factory) -> Path:
    """The `mini` books loaded and days to pay built (with its dbt tests). Read-only."""
    return shared_dir(tmp_path_factory, "dtp-mini", lambda directory: build(directory / "w.duckdb")) / "w.duckdb"


def paid_invoices() -> dict[str, tuple[str, date, int]]:
    """`invoice_id -> (customer, paid on, days to pay)` for every invoice whose payments cover it, from the books."""
    owners = {r["contract_id"]: r["crm_account_id"] for r in csv_rows(MINI_BOOKS / "contracts.csv")}
    invoiced: dict[str, int] = defaultdict(int)
    dated: dict[str, date] = {}
    customer: dict[str, str] = {}
    for r in csv_rows(MINI_BOOKS / "invoices.csv"):
        invoiced[r["invoice_id"]] += int(r["amount_cents"])
        dated[r["invoice_id"]] = date.fromisoformat(r["invoice_date"])
        customer[r["invoice_id"]] = owners[r["contract_id"]]
    applications: dict[str, list[tuple[date, int]]] = defaultdict(list)
    for r in csv_rows(MINI_BOOKS / "payments.csv"):
        applications[r["invoice_id"]].append((date.fromisoformat(r["applied_on"]), int(r["applied_cents"])))
    paid = {}
    for invoice, parts in applications.items():
        total = 0
        for on, cents in sorted(parts):
            total += cents
            if total >= invoiced[invoice]:
                paid[invoice] = (customer[invoice], on, (on - dated[invoice]).days)
                break
    return paid


def expected_rows(months: list[str]) -> dict[tuple[str, str], tuple[int, int, int, bool]]:
    """`(month, customer) -> (paid invoices, median, p80, fallback)` from the books, pooled row included."""
    paid = paid_invoices()
    window, minimum = POLICY.dtp_window_months, POLICY.dtp_min_paid_invoices
    expected = {}
    for month in months:
        in_window = [(customer, days) for customer, on, days in paid.values()
                     if tr.month_of(on) <= month and tr.month_of(on) > tr.months_between("1900-01", month)[-window - 1]]
        pooled = [days for _, days in in_window]
        if not pooled:
            continue
        pool = (tr.lower_median(pooled), tr.percentile_nearest_rank(pooled, 80))
        expected[(month, POOLED)] = (len(pooled), *pool, False)
        for customer in {c for c, _ in in_window}:
            own = [days for c, days in in_window if c == customer]
            if len(own) < minimum:
                expected[(month, customer)] = (len(own), *pool, True)
            else:
                expected[(month, customer)] = (len(own), tr.lower_median(own), tr.percentile_nearest_rank(own, 80), False)
    return expected


def mart(warehouse: Path) -> dict[tuple[str, str], tuple[int, int, int, bool]]:
    return {(month, customer): (n, median, p80, fallback) for month, customer, n, median, p80, fallback in query(
        warehouse, "SELECT month, customer_id, paid_invoices, median_days, p80_days, fallback FROM mart.fct_days_to_pay")}


def test_the_mart_carries_the_standard_columns(mini_dtp):
    columns = [row[0] for row in query(
        mini_dtp, "SELECT column_name FROM information_schema.columns"
                  " WHERE table_schema = 'mart' AND table_name = 'fct_days_to_pay' ORDER BY ordinal_position")]
    assert columns == ["month", "customer_id", "paid_invoices", "median_days", "p80_days", "fallback"]


def test_every_customer_month_equals_the_statistics_computed_from_the_books(mini_dtp):
    months = tr.months_between(*MINI_WINDOW)
    expected = expected_rows(months)
    assert any(customer != POOLED and not fallback for (_, customer), (*_, fallback) in expected.items())
    assert mart(mini_dtp) == expected


def test_a_customer_with_two_paid_invoices_takes_the_pooled_values_and_says_so(mini_dtp):
    rows = mart(mini_dtp)
    short = {key: value for key, value in rows.items() if key[1] != POOLED and value[0] == 2}
    assert short, "mini has no customer-month with exactly two paid invoices"
    for (month, _), (_, median, p80, fallback) in short.items():
        assert fallback is True and (median, p80) == rows[(month, POOLED)][1:3]
    assert not any(fallback for (_, customer), (*_, fallback) in rows.items() if customer == POOLED)
    assert all(fallback == (n < POLICY.dtp_min_paid_invoices) for (_, c), (n, *_, fallback) in rows.items() if c != POOLED)


def test_an_invoice_paid_in_instalments_is_dated_by_its_last_application(mini_dtp):
    paid_on = query(mini_dtp, "SELECT max(applied_on) FROM int.int_cash_application WHERE invoice_id = 'inv_000011'")[0][0]
    assert paid_invoices()["inv_000011"][1:] == (paid_on, 103)
    assert paid_on == date(2025, 5, 14)
    before, after = ("2025-04", "2025-05")
    count = {month: mart(mini_dtp)[(month, POOLED)][0] for month in (before, after)}
    assert count[after] > count[before]


def test_the_provenance_family_records_the_days_and_the_fallback(mini_dtp):
    path = ROOT / "src" / "c2c" / "families" / "dtp.sql"
    month = MINI_WINDOW[1]
    con = db.connect(mini_dtp, read_only=True)
    try:
        rows = prov.family_rows(con, path, month)
        by_id = {row["number_id"]: row for row in rows}
        for (m, customer), (_, median, p80, fallback) in mart(mini_dtp).items():
            if m != month:
                continue
            for metric, days in (("median", median), ("p80", p80)):
                row = by_id[f"dtp/{month}/{customer}/{metric}"]
                assert (row["value_cents"], row["unit"], row["kind"]) == (days, "days", "leaf")
                assert row["value_text"] == f"fallback {str(fallback).lower()}"
                assert con.execute(row["source_query"]).fetchall()
        assert len(by_id) == len(rows) == 2 * sum(1 for m, _ in mart(mini_dtp) if m == month)
    finally:
        con.close()
