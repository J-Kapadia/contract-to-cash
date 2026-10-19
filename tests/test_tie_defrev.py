"""CTL-TIE-DEFREV: deferred revenue ties to GL 2400 to the cent. It passes on the clean `mini` books and
fails the close, naming the ledger entry, the cents and the month, when a recognition entry is changed
(and stays balanced, so CTL-TIE-REV also reports the moved 4000 line) or a 2400 line is moved by a cent."""

from __future__ import annotations

import csv
import shutil
from pathlib import Path

from c2c import db

from conftest import MINI_BOOKS, c2c

MONTH = "2026-01"
CONTROL = "CTL-TIE-DEFREV"
SCRATCH_ENTRY = "je_000345"  # a GL 2400 line of January, billed on inv_000079


def rows(path: Path) -> list[dict]:
    with path.open(encoding="utf-8", newline="") as fh:
        return list(csv.DictReader(fh))


def close(books: Path, warehouse: Path):
    return c2c("close", "--month", MONTH, "--books", str(books), "--warehouse", str(warehouse), "--json")


def assert_named(done, entry_id: str, cents: int, warehouse: Path, also: tuple[str, ...] = ()) -> None:
    assert done.code == 3, done.stderr
    assert [c["id"] for c in done.json["failed_controls"]] == [CONTROL, *also]
    failed = done.json["failed_controls"][0]
    assert failed["violating_rows"] == 1
    [remediation] = failed["remediation"]
    assert entry_id in remediation and f"{cents} cents" in remediation and MONTH in remediation
    assert "GL 2400" in remediation and "{" not in remediation
    assert failed["detail"]["row_hashes"], "the violating GL rows are named by hash"
    assert failed["detail"]["number_ids"]
    assert all(n.startswith(f"defrev/{MONTH}/") and n.endswith("/closing") for n in failed["detail"]["number_ids"])
    with db.connect(warehouse, read_only=True) as con:
        assert con.execute("SELECT status, violating_rows FROM ctl.control_results"
                           " WHERE control_id = ? AND month = ? ORDER BY rowid DESC LIMIT 1",
                           [CONTROL, MONTH]).fetchone() == ("fail", 1)


def test_the_clean_mini_lists_the_control_as_passing(mini_books_copy, workspace):
    done = workspace.run("close", "--month", MONTH, "--json")
    assert done.code == 0, done.stderr
    controls = {entry["id"]: entry for entry in done.json["controls"]}
    assert controls[CONTROL] == {"id": CONTROL, "status": "pass", "violating_rows": 0}


def test_a_changed_recognition_entry_that_stays_balanced_fails_the_close_naming_it(tmp_path):
    out = tmp_path / "copy"
    injected = c2c("inject", "--error", "defrev_gl_slip", "--fixture", "mini", "--out", str(out), "--json",
                   env={"C2C_BOOKS": ""})
    assert injected.code == 0, injected.stderr
    before = {(r["entry_id"], r["line_no"]): r for r in rows(MINI_BOOKS / "gl.csv")}
    changed = [(before[(r["entry_id"], r["line_no"])], r) for r in rows(out / "gl.csv")
               if r["amount_cents"] != before[(r["entry_id"], r["line_no"])]["amount_cents"]]
    [(old, new)] = [pair for pair in changed if pair[0]["gl_account"] == "2400"]
    slip = int(new["amount_cents"]) - int(old["amount_cents"])  # a credit balance: the ledger falls short by this

    warehouse = tmp_path / "w.duckdb"
    done = close(out, warehouse)

    # The altered entry also moves GL 4000 activity, so recognised revenue stops tying in the same month.
    assert_named(done, old["entry_id"], -slip, warehouse, also=("CTL-TIE-REV",))
    revenue = done.json["failed_controls"][1]
    assert revenue["violating_rows"] == 1
    [remediation] = revenue["remediation"]
    assert old["entry_id"] in remediation and f"{slip} cents" in remediation and "GL 4000" in remediation


def test_one_cent_on_a_2400_line_is_named_the_same_way(tmp_path):
    books = tmp_path / "books"
    shutil.copytree(MINI_BOOKS, books)
    table = rows(books / "gl.csv")
    [line] = [r for r in table if r["entry_id"] == SCRATCH_ENTRY and r["gl_account"] == "2400"]
    assert line["gl_period"] == MONTH
    line["amount_cents"] = str(int(line["amount_cents"]) + 1)
    with (books / "gl.csv").open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(table[0]))
        writer.writeheader()
        writer.writerows(table)
    warehouse = tmp_path / "w.duckdb"

    done = close(books, warehouse)

    assert_named(done, SCRATCH_ENTRY, -1, warehouse)
