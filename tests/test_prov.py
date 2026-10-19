"""Provenance records: one per customer-month, a digest that follows the source rows."""

from __future__ import annotations

import csv
import hashlib
import shutil

import pytest

from c2c import db, prov

from conftest import c2c
from control_ids import expected_ids

MONTH = "2026-03"


def test_the_digest_is_over_the_sorted_row_hashes():
    assert prov.digest(["b", "a"]) == hashlib.sha256(b"a\nb").hexdigest() == prov.digest(["a", "b"])


def test_write_leaf_stores_the_record(monkeypatch, tmp_path):
    monkeypatch.setenv("C2C_GIT_SHA", "abc123")
    monkeypatch.delenv("C2C_PRODUCER_KIND", raising=False)
    with db.connect(tmp_path / "t.duckdb") as con:
        prov.write_leaf(con, "ar/2026-03/acct_x/closing", MONTH, 1234, "cents", "demo",
                        "SELECT * FROM (VALUES ('h2'), ('h1')) AS t(_row_hash)", ["CTL-DEMO"], "run1")
        (record,) = con.execute("SELECT * FROM prov.records").fetchall()
    (number_id, month, cents, text, unit, step, sha, kind, digest, count, query, checks, signer, signoff, run) = record
    assert (number_id, month, cents, text, unit, step) == ("ar/2026-03/acct_x/closing", MONTH, 1234, None, "cents", "demo")
    assert (sha, kind, digest, count, checks, signer, signoff, run) == (
        "abc123", "cli", prov.digest(["h1", "h2"]), 2, ["CTL-DEMO"], None, None, "run1")
    assert "VALUES" in query


def test_writing_a_record_again_replaces_it(tmp_path):
    with db.connect(tmp_path / "t.duckdb") as con:
        for value in (1, 2):
            prov.write_leaf(con, "n/1", MONTH, value, "cents", "demo", "SELECT 'h' AS _row_hash")
        assert con.execute("SELECT value_cents FROM prov.records").fetchall() == [(2,)]


METRICS = {"opening": "opening_cents", "billed": "billed_cents", "collected": "collected_cents",
           "credits": "credits_cents", "closing": "closing_cents"}


def test_a_close_writes_the_five_metrics_per_customer_month(closed):
    with db.connect(closed.warehouse, read_only=True) as con:
        mart = {f"ar/{MONTH}/{customer}/{metric}": value
                for customer, *values in con.execute(
                    f"SELECT customer_id, {', '.join(METRICS.values())} FROM mart.fct_ar_rollforward"
                    " WHERE month = ? AND customer_id IS NOT NULL", [MONTH]).fetchall()
                for metric, value in zip(METRICS, values)}
        records = con.execute("SELECT number_id, value_cents, unit, step, input_row_count, checks_passed, signer"
                              " FROM prov.records WHERE number_id LIKE ? ORDER BY 1", [f"ar/{MONTH}/%"]).fetchall()
    assert mart and [r[0] for r in records] == sorted(mart)
    assert len(records) == 5 * len({n.split("/")[2] for n in mart})
    for number_id, cents, unit, step, count, checks, signer in records:
        assert (cents, unit, step, signer) == (mart[number_id], "cents", "fct_ar_rollforward", None)
        assert checks == expected_ids()
        assert count >= 1 or not number_id.endswith("/closing")


def test_the_digest_changes_when_a_source_row_changes(closed, tmp_path):
    books, warehouse = tmp_path / "books", tmp_path / "w.duckdb"
    shutil.copytree(closed.books, books)
    shutil.copy(closed.warehouse, warehouse)

    def digests():
        with db.connect(warehouse, read_only=True) as con:
            return dict(con.execute("SELECT number_id, input_digest FROM prov.records").fetchall())

    before = digests()
    with (books / "payments.csv").open(encoding="utf-8", newline="") as fh:
        rows = list(csv.DictReader(fh))
    edited = rows[0]
    edited["known_on"] = "2026-03-31"  # a column nothing in the model reads, so the money does not move
    with db.connect(warehouse, read_only=True) as con:
        (old_hash,) = con.execute("SELECT _row_hash FROM raw.payments WHERE payment_id = ?",
                                  [edited["payment_id"]]).fetchone()
        reached = reaches(con, old_hash, from_month=edited["applied_on"][:7])

    with (books / "payments.csv").open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)

    done = c2c("close", "--month", MONTH, "--books", str(books), "--warehouse", str(warehouse), "--json")
    assert done.code == 0, done.stderr
    after = digests()

    changed = {n for n in before if before[n] != after[n]}
    # The payment is February's, so February is rebuilt, and March with it: it builds on February.
    assert edited["applied_on"].startswith("2026-02")
    assert changed == reached
    assert {n for n in changed if n.startswith(f"ar/{MONTH}/") and n.endswith("/closing")}
    assert set(before) == set(after)


def reaches(con, row_hash: str, from_month: str) -> set[str]:
    """The numbers that select the source row `row_hash`, from the families' own queries: a leaf whose
    `source_query` returns it, and a composite with such a number among its children. Months before
    `from_month` cannot select it, so they are not run, and the month-free reference and configuration
    files read none of the books."""
    months = [m for (m,) in con.execute("SELECT DISTINCT month FROM prov.records WHERE month >= ? ORDER BY 1",
                                        [from_month]).fetchall()]
    rows = {}
    for path in prov.month_families():
        if path.stem not in ("ref", "cfg"):
            for month in months:
                rows.update({row["number_id"]: row for row in prov.family_rows(con, path, month)})
    hit: dict[str, bool] = {}

    def reached(number_id: str) -> bool:
        if number_id not in hit:
            row = rows[number_id]
            if row["kind"] == "leaf":
                hit[number_id] = (row_hash,) in con.execute(row["source_query"]).fetchall()
            else:
                hit[number_id] = any(reached(child) for child in row["children"])
        return hit[number_id]

    return {number_id for number_id in rows if reached(number_id)}
