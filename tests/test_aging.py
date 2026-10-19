"""Aged receivables over the `mini` books.

The buckets of a customer add up to its closing receivables, an invoice 45 days past due at month-end
lands in 31-60, and the provenance family records every row and every bucket total.
"""

from __future__ import annotations

import csv
import shutil
from pathlib import Path

import pytest

from c2c import db, dbt_runner, load, prov
from conftest import DEFAULT_CONFIG, MINI_BOOKS, MINI_WINDOW, ROOT, shared_dir

REFERENCE = ROOT / "data" / "reference"
BUCKETS = ["1-30", "31-60", "61-90", "91+", "current"]
MONTH = MINI_WINDOW[1]
# One more invoice of an existing customer: billed 2026-02-01, due 2026-02-14, 45 days before month-end.
HANDMADE = ("inv_x_000001", "1", "bill_0002", "ctr_0002_00", "ctr_0002_00_L1", "2026-02-01", "2026-02", "2026-02-14",
            "2026-02-01", "2026-02-28", "subscription", "123400", "2026-02-01")


def build(warehouse: Path, books: Path = MINI_BOOKS) -> None:
    con = db.connect(warehouse)
    try:
        db.init_schemas(con)
        load.load_months(con, books, *MINI_WINDOW, "test")
        load.load_reference(con, REFERENCE)
        load.load_config(con, DEFAULT_CONFIG)
    finally:
        con.close()
    dbt_runner.build(warehouse, select="+fct_ar_aging")


def query(warehouse: Path, sql: str, params=()):
    con = db.connect(warehouse, read_only=True)
    try:
        return con.execute(sql, params).fetchall()
    finally:
        con.close()


@pytest.fixture(scope="module")
def mini_aging(tmp_path_factory) -> Path:
    """The `mini` books loaded and aging built (with its dbt tests). Read-only."""
    return shared_dir(tmp_path_factory, "aging-mini", lambda directory: build(directory / "w.duckdb")) / "w.duckdb"


@pytest.fixture(scope="module")
def handmade(tmp_path_factory) -> Path:
    """The `mini` books plus the handmade invoice, aging built. Read-only."""

    def make(directory: Path) -> None:
        books = directory / "books"
        shutil.copytree(MINI_BOOKS, books)
        with (books / "invoices.csv").open("a", encoding="utf-8", newline="") as fh:
            csv.writer(fh, lineterminator="\n").writerow(HANDMADE)
        build(directory / "w.duckdb", books)

    return shared_dir(tmp_path_factory, "aging-mini-handmade", make) / "w.duckdb"


def aging(warehouse: Path, month: str = MONTH) -> dict[tuple[str, str], int]:
    return {(customer, bucket): cents for customer, bucket, cents in query(
        warehouse, "SELECT customer_id, bucket, open_cents FROM mart.fct_ar_aging WHERE month = ?", [month])}


def test_the_mart_carries_the_standard_columns(mini_aging):
    columns = [row[0] for row in query(
        mini_aging, "SELECT column_name FROM information_schema.columns"
                    " WHERE table_schema = 'mart' AND table_name = 'fct_ar_aging' ORDER BY ordinal_position")]
    assert columns == ["month", "customer_id", "bucket", "open_cents"]


def test_every_customer_month_has_every_bucket(mini_aging):
    rows = query(mini_aging, "SELECT month, customer_id, list_sort(list(bucket)) FROM mart.fct_ar_aging GROUP BY ALL")
    assert rows and all(buckets == BUCKETS for *_, buckets in rows)
    assert {(m, c) for m, c, _ in rows} == {(m, c) for m, c in query(
        mini_aging, "SELECT month, customer_id FROM mart.fct_ar_rollforward")}


def test_the_buckets_of_a_customer_add_up_to_its_closing_receivables(mini_aging):
    closing = {(m, c): cents for m, c, cents in query(
        mini_aging, "SELECT month, customer_id, closing_cents FROM mart.fct_ar_rollforward")}
    summed = {(m, c): cents for m, c, cents in query(
        mini_aging, "SELECT month, customer_id, sum(open_cents) FROM mart.fct_ar_aging GROUP BY ALL")}
    assert summed == closing
    assert sum(1 for (m, _), cents in closing.items() if m == MONTH and cents) > 1


def test_an_invoice_45_days_past_due_lands_in_31_60(mini_aging, handmade):
    before, after = aging(mini_aging), aging(handmade)
    delta = {key: after[key] - before.get(key, 0) for key in after if after[key] != before.get(key, 0)}
    ((_, bucket), cents), = delta.items()
    assert (bucket, cents) == ("31-60", 123400)


def test_the_provenance_family_records_every_row_and_every_bucket_total(mini_aging):
    path = ROOT / "src" / "c2c" / "families" / "aging.sql"
    con = db.connect(mini_aging, read_only=True)
    try:
        rows = prov.family_rows(con, path, MONTH)
        by_id = {row["number_id"]: row for row in rows}
        for (customer, bucket), cents in aging(mini_aging).items():
            row = by_id[f"aging/{MONTH}/{customer}/{bucket}"]
            assert (row["value_cents"], row["unit"], row["kind"]) == (cents, "cents", "leaf")
            assert con.execute(row["source_query"]).fetchall()
        for bucket in BUCKETS:
            row = by_id[f"aging/{MONTH}/{bucket}"]
            leaves = [r for r in rows if r["kind"] == "leaf" and r["number_id"].endswith("/" + bucket)]
            assert row["kind"] == "composite"
            assert sorted(row["children"]) == sorted(r["number_id"] for r in leaves)
            assert row["value_cents"] == sum(r["value_cents"] for r in leaves)
        assert len(rows) == len(aging(mini_aging)) + len(BUCKETS)
    finally:
        con.close()
