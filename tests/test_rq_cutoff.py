"""The cutoff findings: none on a closed month, because the control stops the close, and on a close it
stopped, one mart row and one registered number per invoice and period involved."""

from __future__ import annotations

import csv
from pathlib import Path

from c2c import db, prov

from conftest import MINI_BOOKS, MINI_WINDOW, c2c
from forecast_helpers import mini_closed  # noqa: F401  (fixture)

MOVED_INVOICE, INVOICE_MONTH, MOVED_TO = "inv_000070", "2025-12", "2026-01"


def months(first: str, last: str) -> list[str]:
    out, (year, month) = [], map(int, first.split("-"))
    while f"{year}-{month:02d}" <= last:
        out.append(f"{year}-{month:02d}")
        year, month = (year + 1, 1) if month == 12 else (year, month + 1)
    return out


def query(warehouse: Path, sql: str, params=()):
    with db.connect(warehouse, read_only=True) as con:
        return con.execute(sql, params).fetchall()


def test_a_closed_month_has_no_finding(mini_closed):
    assert query(mini_closed, "SELECT count(*) FROM mart.fct_cutoff_findings") == [(0,)]
    stored = dict(query(mini_closed, "SELECT number_id, value_cents FROM prov.records WHERE number_id LIKE 'cutoff/%'"))
    closed = months(*MINI_WINDOW)
    assert closed[0] == "2024-10" and closed[-1] == "2026-03"
    assert stored == {f"cutoff/{month}/findings": 0 for month in closed}


def test_explain_resolves_the_count_of_a_closed_month(mini_closed, tmp_path):
    done = c2c("explain", f"cutoff/{MINI_WINDOW[1]}/findings", "--json", "--books", str(MINI_BOOKS),
               "--warehouse", str(mini_closed))
    assert done.code == 0, done.stderr
    assert done.json["explanation"]["record"]["step"] == "fct_cutoff_findings"


def test_a_stopped_close_records_the_invoice_and_the_periods(mini_books_copy, workspace):
    path = mini_books_copy / "gl.csv"
    with path.open(encoding="utf-8", newline="") as fh:
        gl = list(csv.DictReader(fh))
    moved = [row for row in gl if row["source_ref"] == MOVED_INVOICE]
    assert moved, "the invoice has GL entries"
    debit = sum(int(row["amount_cents"]) for row in moved if int(row["amount_cents"]) > 0)
    # The invoice posts to receivables (1200) against deferred revenue; the debit is the receivable.
    assert len(moved) == 2 and debit == int(next(r for r in moved if r["gl_account"] == "1200")["amount_cents"])
    for row in moved:
        row["gl_period"] = MOVED_TO
    with path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(gl[0]))
        writer.writeheader()
        writer.writerows(gl)

    done = workspace.run("close", "--month", MOVED_TO, "--json")

    assert done.code == 3, done.stderr
    assert "CTL-CUTOFF" in {entry["id"] for entry in done.json["failed_controls"]}
    assert query(workspace.warehouse,
                 "SELECT month, invoice_id, invoice_month, gl_period, entries, amount_cents"
                 " FROM mart.fct_cutoff_findings") == [
        (INVOICE_MONTH, MOVED_INVOICE, INVOICE_MONTH, MOVED_TO, len(moved), debit)]

    with db.connect(workspace.warehouse, read_only=True) as con:
        for month in (INVOICE_MONTH, MOVED_TO):
            rows = {r["number_id"]: r for r in prov.family_rows(con, prov.FAMILIES_DIR / "cutoff.sql", month)}
            assert set(rows) == {f"cutoff/{month}/findings", f"cutoff/{month}/{MOVED_INVOICE}/{MOVED_TO}"}
            assert rows[f"cutoff/{month}/findings"]["value_cents"] == 1
            leaf = rows[f"cutoff/{month}/{MOVED_INVOICE}/{MOVED_TO}"]
            assert leaf["value_cents"] == debit and leaf["unit"] == "cents"
            assert len(con.execute(leaf["source_query"]).fetchall()) == len(moved)
            assert len(con.execute(rows[f"cutoff/{month}/findings"]["source_query"]).fetchall()) == len(moved)
