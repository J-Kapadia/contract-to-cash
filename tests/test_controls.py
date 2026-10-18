"""Controls: a violating row fails the close with the control's remediation, filled from the row."""

from __future__ import annotations

import csv
import shutil

import pytest

from c2c import controls, db
from c2c.errors import ControlFailure, InternalError

from conftest import c2c

MONTH = "2026-03"
EDITED_ENTRY = "je_000002"  # a GL 1200 line of January


def edit_gl_line(books, entry_id: str, extra_cents: int) -> None:
    path = books / "gl.csv"
    with path.open(encoding="utf-8", newline="") as fh:
        rows = list(csv.DictReader(fh))
    for row in rows:
        if row["entry_id"] == entry_id and row["gl_account"] == "1200":
            row["amount_cents"] = str(int(row["amount_cents"]) + extra_cents)
    with path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def test_a_clean_close_reports_every_mart_control_as_passing(closed):
    done = c2c("close", "--month", MONTH, "--books", str(closed.books), "--warehouse", str(closed.warehouse),
               "--json")
    assert done.code == 0, done.stderr
    assert done.json["controls"] == [{"id": "CTL-TIE-AR", "status": "pass", "violating_rows": 0}]


def test_editing_a_gl_1200_line_fails_the_close_with_a_remediation_naming_it(closed, tmp_path):
    books, warehouse = tmp_path / "books", tmp_path / "w.duckdb"
    shutil.copytree(closed.books, books)
    shutil.copy(closed.warehouse, warehouse)
    edit_gl_line(books, EDITED_ENTRY, 1)

    done = c2c("close", "--month", MONTH, "--books", str(books), "--warehouse", str(warehouse), "--json")

    assert done.code == 3, done.stderr
    [failed] = done.json["failed_controls"]
    assert failed["id"] == "CTL-TIE-AR"
    assert failed["violating_rows"] == 1
    [remediation] = failed["remediation"]
    assert EDITED_ENTRY in remediation
    assert "1 cents" in remediation and "2026-01" in remediation and "GL 1200" in remediation
    assert "{" not in remediation
    assert failed["detail"]["row_hashes"], "the violating GL rows are named by hash"
    assert all(n.startswith("ar/2026-01/") and n.endswith("/closing") for n in failed["detail"]["number_ids"])

    with db.connect(warehouse, read_only=True) as con:
        assert con.execute("SELECT status, violating_rows FROM ctl.control_results"
                           " WHERE control_id = 'CTL-TIE-AR' AND month = ? ORDER BY rowid DESC LIMIT 1",
                           [MONTH]).fetchone() == ("fail", 1)


def test_a_failed_close_does_not_write_provenance_for_the_month(closed, tmp_path):
    books, warehouse = tmp_path / "books", tmp_path / "w.duckdb"
    shutil.copytree(closed.books, books)
    edit_gl_line(books, EDITED_ENTRY, 5)
    done = c2c("close", "--month", MONTH, "--books", str(books), "--warehouse", str(warehouse), "--json")
    assert done.code == 3, done.stderr
    with db.connect(warehouse, read_only=True) as con:
        assert con.execute("SELECT count(*) FROM information_schema.tables"
                           " WHERE table_schema = 'prov' AND table_name = 'records'").fetchone() == (0,)


def control_dir(tmp_path, body: str):
    directory = tmp_path / "controls"
    directory.mkdir()
    (directory / "CTL-DEMO.sql").write_text(body, encoding="utf-8")
    return directory


def test_a_placeholder_that_is_not_a_returned_column_fails_fast(tmp_path):
    directory = control_dir(tmp_path, "-- remediation: Fix {row_id} and {missing_column}.\n-- stage: mart\n"
                                      "select 'r1' as row_id where false\n")
    with db.connect(tmp_path / "t.duckdb") as con, pytest.raises(InternalError) as raised:
        controls.run(con, MONTH, "mart", directory=directory)
    assert "{missing_column}" in raised.value.message and "CTL-DEMO" in raised.value.message


def test_a_control_without_a_remediation_line_fails_fast(tmp_path):
    directory = control_dir(tmp_path, "-- stage: mart\nselect 1 as x where false\n")
    with db.connect(tmp_path / "t.duckdb") as con, pytest.raises(InternalError, match="remediation"):
        controls.run(con, MONTH, "mart", directory=directory)


def test_the_month_is_bound_as_a_parameter_and_other_stages_are_left_alone(tmp_path):
    directory = control_dir(tmp_path, "-- remediation: Month {month} has a problem in {thing}.\n-- stage: mart\n"
                                      "select $month as month, 'x' as thing\n")
    (directory / "CTL-OTHER.sql").write_text("-- remediation: never.\n-- stage: raw\nselect 1 as one\n",
                                             encoding="utf-8")
    with db.connect(tmp_path / "t.duckdb") as con:
        with pytest.raises(ControlFailure) as raised:
            controls.run(con, "2026-02", "mart", directory=directory)
        assert raised.value.exit == 3
        assert raised.value.failed_controls[0]["remediation"] == ["Month 2026-02 has a problem in x."]
        assert con.execute("SELECT control_id, status, violating_rows FROM ctl.control_results").fetchall() == [
            ("CTL-DEMO", "fail", 1)]
