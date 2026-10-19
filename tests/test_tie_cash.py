"""CTL-TIE-CASH: the cash roll-forward ties to GL 1000 to the cent. It passes on the clean `mini` books
and fails the close, naming the ledger entry, the cents and the month, when a 1000 line is moved by a
cent; the receivables tie-out is not named."""

from __future__ import annotations

import csv
import shutil
from pathlib import Path

from c2c import db

from conftest import MINI_BOOKS, c2c

MONTH = "2026-03"
CONTROL = "CTL-TIE-CASH"
SCRATCH_ENTRY = "je_000404"  # a GL 1000 line of March: the receipt of pay_000080


def test_the_clean_mini_lists_the_control_as_passing(mini_books_copy, workspace):
    done = workspace.run("close", "--month", MONTH, "--json")
    assert done.code == 0, done.stderr
    controls = {entry["id"]: entry for entry in done.json["controls"]}
    assert controls[CONTROL] == {"id": CONTROL, "status": "pass", "violating_rows": 0}


def test_one_cent_on_a_1000_line_fails_the_close_naming_it(tmp_path):
    books = tmp_path / "books"
    shutil.copytree(MINI_BOOKS, books)
    with (books / "gl.csv").open(encoding="utf-8", newline="") as fh:
        table = list(csv.DictReader(fh))
    [line] = [r for r in table if r["entry_id"] == SCRATCH_ENTRY and r["gl_account"] == "1000"]
    assert line["gl_period"] == MONTH
    line["amount_cents"] = str(int(line["amount_cents"]) + 1)
    with (books / "gl.csv").open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(table[0]))
        writer.writeheader()
        writer.writerows(table)
    warehouse: Path = tmp_path / "w.duckdb"

    # the harness points C2C_BOOKS at a private root, which would win over the fixture's books
    done = c2c("close", "--month", MONTH, "--fixture", "mini", "--books", str(books),
               "--warehouse", str(warehouse), "--json")

    assert done.code == 3, done.stderr
    assert [c["id"] for c in done.json["failed_controls"]] == [CONTROL]
    assert "CTL-TIE-AR" not in done.stderr and "CTL-TIE-AR" not in done.stdout
    [failed] = done.json["failed_controls"]
    assert failed["violating_rows"] == 1
    [remediation] = failed["remediation"]
    assert SCRATCH_ENTRY in remediation and "1 cents" in remediation and MONTH in remediation
    assert "GL 1000" in remediation and "{" not in remediation
    assert failed["detail"]["row_hashes"], "the violating GL rows are named by hash"
    assert failed["detail"]["number_ids"] == [f"cash/{MONTH}/closing"]
    with db.connect(warehouse, read_only=True) as con:
        assert con.execute("SELECT status, violating_rows FROM ctl.control_results"
                           " WHERE control_id = ? AND month = ? ORDER BY rowid DESC LIMIT 1",
                           [CONTROL, MONTH]).fetchone() == ("fail", 1)
