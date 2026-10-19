"""CTL-TIE-REV: recognised revenue ties to the GL 4000 and 4100 activity of each closed month. It passes
on the clean `mini` books and fails the close, naming the ledger entry, the cents, the account and the
month, when a revenue line of the ledger is moved by a cent."""

from __future__ import annotations

import csv
import shutil
from pathlib import Path

from c2c import db

from conftest import MINI_BOOKS, c2c

MONTH = "2026-03"
CONTROL = "CTL-TIE-REV"
USAGE_ENTRY = "je_000412"  # the GL 4100 line of inv_000095, billed in March
SUBSCRIPTION_ENTRY = "je_000419"  # the GL 4000 line recognising inv_000066 in March


def rows(path: Path) -> list[dict]:
    with path.open(encoding="utf-8", newline="") as fh:
        return list(csv.DictReader(fh))


def books_with_one_more_cent_of_revenue(tmp_path: Path, entry_id: str, account: str) -> Path:
    """A copy of the mini books where the ledger's revenue line carries one more cent (a credit is
    negative, so the amount falls by one) and the ledger exceeds the sub-ledger by one cent."""
    books = tmp_path / "books"
    shutil.copytree(MINI_BOOKS, books)
    table = rows(books / "gl.csv")
    [line] = [r for r in table if r["entry_id"] == entry_id and r["gl_account"] == account]
    assert line["gl_period"] == MONTH
    line["amount_cents"] = str(int(line["amount_cents"]) - 1)
    with (books / "gl.csv").open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(table[0]))
        writer.writeheader()
        writer.writerows(table)
    return books


def assert_named(tmp_path: Path, entry_id: str, account: str, line_types: set[str]) -> None:
    books = books_with_one_more_cent_of_revenue(tmp_path, entry_id, account)
    warehouse = tmp_path / "w.duckdb"

    done = c2c("close", "--month", MONTH, "--books", str(books), "--warehouse", str(warehouse), "--json")

    assert done.code == 3, done.stderr
    assert [c["id"] for c in done.json["failed_controls"]] == [CONTROL]
    [failed] = done.json["failed_controls"]
    assert failed["violating_rows"] == 1
    [remediation] = failed["remediation"]
    assert entry_id in remediation and "1 cents" in remediation and MONTH in remediation
    assert f"GL {account}" in remediation and "{" not in remediation
    assert failed["detail"]["row_hashes"], "the violating GL rows are named by hash"
    assert failed["detail"]["number_ids"]
    for number_id in failed["detail"]["number_ids"]:
        _, month, _customer, line_type = number_id.split("/")
        assert month == MONTH and line_type in line_types
    with db.connect(warehouse, read_only=True) as con:
        assert con.execute("SELECT status, violating_rows FROM ctl.control_results"
                           " WHERE control_id = ? AND month = ? ORDER BY rowid DESC LIMIT 1",
                           [CONTROL, MONTH]).fetchone() == ("fail", 1)


def test_the_clean_mini_lists_the_control_as_passing(mini_books_copy, workspace):
    done = workspace.run("close", "--month", MONTH, "--json")
    assert done.code == 0, done.stderr
    controls = {entry["id"]: entry for entry in done.json["controls"]}
    assert controls[CONTROL] == {"id": CONTROL, "status": "pass", "violating_rows": 0}


def test_one_cent_on_a_4100_line_is_named_with_the_account_and_the_entry(tmp_path):
    assert_named(tmp_path, USAGE_ENTRY, "4100", {"overage"})


def test_one_cent_on_a_4000_line_is_named_with_the_account_and_the_entry(tmp_path):
    assert_named(tmp_path, SUBSCRIPTION_ENTRY, "4000", {"subscription", "credit"})
