"""Provenance records: one per customer-month, a digest that follows the source rows."""

from __future__ import annotations

import csv
import hashlib
import shutil

import pytest

from c2c import db, prov

from conftest import c2c

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


def test_a_close_writes_one_record_per_customer_month(closed):
    with db.connect(closed.warehouse, read_only=True) as con:
        customers = [r[0] for r in con.execute(
            "SELECT customer_id FROM mart.fct_ar_rollforward WHERE month = ? ORDER BY 1", [MONTH]).fetchall()]
        records = con.execute("SELECT number_id, value_cents, unit, step, input_row_count, checks_passed, signer"
                              " FROM prov.records WHERE number_id LIKE ? ORDER BY 1", [f"ar/{MONTH}/%"]).fetchall()
        closing = dict(con.execute("SELECT customer_id, closing_cents FROM mart.fct_ar_rollforward"
                                   " WHERE month = ?", [MONTH]).fetchall())
    assert customers and [r[0] for r in records] == [f"ar/{MONTH}/{c}/closing" for c in customers]
    for number_id, cents, unit, step, count, checks, signer in records:
        customer = number_id.split("/")[2]
        assert (cents, unit, step, signer) == (closing[customer], "cents", "fct_ar_rollforward", None)
        assert count >= 1 and checks == ["CTL-BILL-CONTRACT", "CTL-CUTOFF", "CTL-TIE-AR"]


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
    with (books / "payments.csv").open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)

    done = c2c("close", "--month", MONTH, "--books", str(books), "--warehouse", str(warehouse), "--json")
    assert done.code == 0, done.stderr
    after = digests()

    with db.connect(warehouse, read_only=True) as con:
        (owner,) = con.execute(
            "SELECT min(c.crm_account_id) FROM raw.invoices i JOIN raw.contracts c USING (contract_id)"
            " WHERE i.invoice_id = ?", [edited["invoice_id"]]).fetchone()
    changed = {n for n in before if before[n] != after[n]}
    assert changed == {f"ar/{MONTH}/{owner}/closing"}
    assert set(before) == set(after)
