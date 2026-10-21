"""`c2c load-tb`: a directory of monthly trial balance exports stacked into `mart.fct_trial_balance`
and one CSV, and checked on load. The `qbo` fixtures carry the planted faults: 2025-06 is missing,
2025-11 does not balance, and account 6900 appears in 2025-09."""

from __future__ import annotations

import csv
import shutil
from pathlib import Path

from c2c import db
from c2c.intake.trial_balance import parse

from conftest import c2c

TB = Path(__file__).parent / "fixtures" / "tb"
QBO = TB / "qbo"
CONTROLS = ["CTL-TB-ACCOUNT-CHANGE", "CTL-TB-BALANCES", "CTL-TB-PERIOD-GAP", "CTL-TB-TOTAL"]


def run(tmp_path: Path, directory: Path):
    done = c2c("load-tb", str(directory), "--warehouse", str(tmp_path / "w.duckdb"),
               "--csv", str(tmp_path / "tb.csv"), "--json")
    return done, tmp_path / "w.duckdb", tmp_path / "tb.csv"


def files_before_the_gap(tmp_path: Path) -> Path:
    directory = tmp_path / "before_gap"
    directory.mkdir()
    for path in sorted(QBO.glob("*.csv")):
        if path.stem.removeprefix("trial_balance_") < "2025-06":
            shutil.copy(path, directory)
    return directory


def failed(envelope: dict) -> dict[str, dict]:
    return {c["id"]: c for c in envelope["failed_controls"]}


def test_the_planted_faults_each_fail_their_own_control(tmp_path):
    done, warehouse, csv_path = run(tmp_path, QBO)
    assert done.code == 3, done.stderr
    envelope = done.json
    controls = failed(envelope)
    assert set(controls) == {"CTL-TB-BALANCES", "CTL-TB-PERIOD-GAP", "CTL-TB-ACCOUNT-CHANGE"}
    (balances,) = controls["CTL-TB-BALANCES"]["remediation"]
    assert "2025-11" in balances and "12345" in balances
    (gap,) = controls["CTL-TB-PERIOD-GAP"]["remediation"]
    assert "2025-06" in gap
    (change,) = controls["CTL-TB-ACCOUNT-CHANGE"]["remediation"]
    assert "6900" in change and "appears" in change and "2025-09" in change
    assert all("{" not in step for c in controls.values() for step in c["remediation"])
    assert "CTL-TB-TOTAL" not in controls  # every fixture's Total row equals its sums, 2025-11 included

    expected = sum(len(parse(p).rows) for p in QBO.glob("*.csv"))
    con = db.connect(warehouse, read_only=True)
    try:
        assert con.execute("select count(*) from mart.fct_trial_balance").fetchone()[0] == expected
        assert con.execute("select count(*) from mart.fct_trial_balance_totals").fetchone()[0] == 24
        recorded = {i: s for i, s in con.execute(
            "select control_id, status from ctl.control_results where month = '2026-03'").fetchall()}
    finally:
        con.close()
    assert recorded["CTL-TB-TOTAL"] == "pass"
    with open(csv_path, newline="", encoding="utf-8") as fh:
        table = list(csv.DictReader(fh))
    assert len(table) == expected
    assert list(table[0]) == ["period", "account_id", "account_name", "debit_cents", "credit_cents",
                              "net_cents", "source_file", "_row_hash"]
    assert [(r["period"], r["account_id"]) for r in table] == sorted((r["period"], r["account_id"]) for r in table)


def test_files_before_the_gap_load_and_pass_every_control(tmp_path):
    directory = files_before_the_gap(tmp_path)
    done, warehouse, csv_path = run(tmp_path, directory)
    assert done.code == 0, done.stderr
    envelope = done.json
    assert envelope["command"] == "load-tb"
    assert envelope["month"] == "2025-05"
    assert envelope["periods"] == sorted(envelope["periods"])
    assert envelope["periods"][0] == "2024-03" and envelope["periods"][-1] == "2025-05"
    assert len(envelope["periods"]) == envelope["files"] == 15
    assert envelope["csv"] == str(csv_path)
    assert {c["id"]: c["status"] for c in envelope["controls"]} == {c: "pass" for c in CONTROLS}
    con = db.connect(warehouse, read_only=True)
    try:
        rows = con.execute("select count(*), sum(net_cents) from mart.fct_trial_balance").fetchone()
    finally:
        con.close()
    assert rows == (envelope["rows"], 0)


def test_a_total_row_that_differs_from_its_rows_fails_alone(tmp_path):
    directory = files_before_the_gap(tmp_path)
    target = directory / "trial_balance_2024-09.csv"
    with open(target, newline="", encoding="utf-8") as fh:
        table = list(csv.reader(fh))
    (total,) = [row for row in table if row and row[0].lower().startswith("total")]
    debit, credit = total[1], total[2]
    total[1] = f"{(parse(target).total_debit_cents + 100) / 100:,.2f}"
    with open(target, "w", newline="", encoding="utf-8") as fh:
        csv.writer(fh, lineterminator="\n").writerows(table)
    done, _, _ = run(tmp_path, directory)
    assert done.code == 3, done.stderr
    controls = failed(done.json)
    assert list(controls) == ["CTL-TB-TOTAL"]
    (step,) = controls["CTL-TB-TOTAL"]["remediation"]
    assert "2024-09" in step and total[1].replace(",", "").replace(".", "") in step
    assert debit.replace(",", "").replace(".", "") in step and credit.replace(",", "").replace(".", "") in step


def test_two_files_for_one_period_fail_before_anything_is_written(tmp_path):
    directory = files_before_the_gap(tmp_path)
    shutil.copy(directory / "trial_balance_2024-05.csv", directory / "trial_balance_2024-05_copy.csv")
    done, warehouse, csv_path = run(tmp_path, directory)
    assert done.code == 2
    message = done.json["message"]
    assert "trial_balance_2024-05.csv" in message and "trial_balance_2024-05_copy.csv" in message
    assert "2024-05" in message
    assert not csv_path.exists()
    if warehouse.exists():
        con = db.connect(warehouse, read_only=True)
        try:
            assert con.execute("select count(*) from information_schema.tables"
                               " where table_name = 'fct_trial_balance'").fetchone()[0] == 0
        finally:
            con.close()


def test_the_two_layouts_of_the_same_months_are_a_duplicate_period(tmp_path):
    mixed = tmp_path / "mixed"
    mixed.mkdir()
    for path in (TB / "qbo_xlsx").iterdir():
        shutil.copy(path, mixed / path.name)
    for path in (TB / "netsuite").glob("tb_2026-*"):
        shutil.copy(path, mixed / path.name)
    done, _, _ = run(tmp_path, mixed)
    assert done.code == 2
    assert "2026-01" in done.json["message"]


def test_the_netsuite_months_load(tmp_path):
    # tb_latest.csv repeats tb_2026-03.xlsx under another name, which is a duplicate period, so the
    # directory is read without it.
    directory = tmp_path / "netsuite"
    directory.mkdir()
    for path in (TB / "netsuite").glob("tb_2026-*"):
        shutil.copy(path, directory / path.name)
    done, _, _ = run(tmp_path, directory)
    assert done.code == 0, done.stderr
    assert done.json["periods"] == ["2026-01", "2026-02", "2026-03"]


def test_the_netsuite_directory_as_shipped_repeats_march(tmp_path):
    done, _, _ = run(tmp_path, TB / "netsuite")
    assert done.code == 2
    assert "tb_2026-03.xlsx" in done.json["message"] and "tb_latest.csv" in done.json["message"]


def test_the_command_name_has_a_hyphen():
    listed = c2c("--help").stdout
    assert "load-tb" in listed and "load_tb" not in listed
