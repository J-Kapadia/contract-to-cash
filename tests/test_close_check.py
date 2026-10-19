"""`close --check` over the `mini` books: the closed results against the answer key, read-only."""

from __future__ import annotations

import csv
import json
import shutil
from pathlib import Path

import pytest

from c2c import check, db, dbt_runner, state
from conftest import MINI_BOOKS, MINI_TRUTH, MINI_WINDOW, ROOT, c2c, shared_dir

MONTH = "2026-03"
EXPECTED = ROOT / "tests" / "fixtures" / "expected"
FIGURES = ("revenue_cents", "defrev_opening_cents", "defrev_closing_cents", "ar_opening_cents", "ar_closing_cents")
CHECK_IDS = ["CHK-TRUTH-ROLLFORWARD", "CHK-TRUTH-IDENTITIES", "CHK-PROV-COVERAGE"]


def close(warehouse: Path, *extra: str, truth: Path = MINI_TRUTH):
    # the harness points C2C_BOOKS at a private root, which would win over the fixture's books
    return c2c("close", "--month", MONTH, "--fixture", "mini", "--books", str(MINI_BOOKS), "--truth", str(truth),
               "--warehouse", str(warehouse), "--check", "--json", *extra)


def rows(path: Path) -> list[dict]:
    with path.open(encoding="utf-8", newline="") as fh:
        return list(csv.DictReader(fh))


def query(warehouse: Path, sql: str, params=()):
    con = db.connect(warehouse, read_only=True)
    try:
        return con.execute(sql, params).fetchall()
    finally:
        con.close()


@pytest.fixture(scope="module")
def checked(tmp_path_factory):
    """`mini` closed through 2026-03 with `--check`: its warehouse and the first run's envelope. Read-only:
    a test that runs `c2c` on the warehouse takes a copy, since a run holds the warehouse lock."""

    def build(directory: Path) -> None:
        done = close(directory / "w.duckdb")
        assert done.code == 0, done.stderr
        (directory / "first.json").write_text(done.stdout, encoding="utf-8")

    root = shared_dir(tmp_path_factory, "close-check-mini", build)
    return root / "w.duckdb", json.loads((root / "first.json").read_text(encoding="utf-8"))


@pytest.fixture
def warehouse_copy(checked, tmp_path) -> Path:
    shutil.copy(checked[0], tmp_path / "w.duckdb")
    return tmp_path / "w.duckdb"


def test_a_clean_close_passes_every_control_and_check(checked):
    envelope = checked[1]
    assert envelope["status"] == "ok" and envelope["exit"] == 0
    controls = envelope["controls"]
    assert {c["status"] for c in controls} == {"pass"}
    assert [c["id"] for c in controls][-3:] == CHECK_IDS
    assert len(controls) > 3


def test_catch_up_closed_every_earlier_month(checked):
    first, last = MINI_WINDOW
    got = dict(query(checked[0], "SELECT month, status FROM state.close_state"))
    assert got == {m: "closed" for m in state.months(first, last)}


def test_balances_equal_the_expected_rollforward(checked):
    mart: dict[tuple[str, str], dict[str, int]] = {}

    def put(table: str, column: str, name: str) -> None:
        for customer, month, cents in query(
                checked[0], f"SELECT customer_id, month, sum({column})::bigint FROM mart.{table} GROUP BY ALL"):
            mart.setdefault((customer, month), {})[name] = cents

    put("fct_revenue", "recognised_cents", "revenue_cents")
    put("fct_defrev_rollforward", "opening_cents", "defrev_opening_cents")
    put("fct_defrev_rollforward", "closing_cents", "defrev_closing_cents")
    put("fct_ar_rollforward", "opening_cents", "ar_opening_cents")
    put("fct_ar_rollforward", "closing_cents", "ar_closing_cents")
    expected = {(r["customer_id"], r["month"]): {c: int(r[c]) for c in FIGURES}
                for r in rows(EXPECTED / "rollforward.csv")}
    for key in set(expected) | set(mart):
        assert {c: mart.get(key, {}).get(c, 0) for c in FIGURES} == expected.get(key, dict.fromkeys(FIGURES, 0)), key


def test_the_bridge_equals_the_expected_bridge(checked):
    got = query(checked[0], "SELECT month, line, cents FROM mart.fct_bridge ORDER BY month, line")
    expected = sorted((r["month"], line, int(r[line])) for r in rows(EXPECTED / "bridge.csv")
                      for line in ("revenue", "defrev_movement", "net_billings", "ar_movement", "receipts"))
    assert got == expected


def test_two_runs_give_the_same_result_hash(checked, warehouse_copy, tmp_path):
    again = close(warehouse_copy)  # the same warehouse: nothing changed
    assert again.code == 0, again.stderr
    other = close(tmp_path / "other.duckdb")  # a fresh one
    assert other.code == 0, other.stderr
    assert checked[1]["result_hash"] == again.json["result_hash"] == other.json["result_hash"]


def test_dbt_test_passes_on_the_closed_warehouse(warehouse_copy):
    dbt_runner.test(warehouse_copy)


def test_check_changes_no_state_and_no_mart(warehouse_copy):
    before = warehouse_copy.read_bytes()
    con = db.connect(warehouse_copy, read_only=True)
    try:
        passed = check.close_check(con, MONTH, MINI_TRUTH)
    finally:
        con.close()
    assert [c["id"] for c in passed] == CHECK_IDS
    assert warehouse_copy.read_bytes() == before


def scratch_truth(tmp_path: Path, name: str, edit) -> Path:
    truth = tmp_path / "truth"
    shutil.copytree(MINI_TRUTH, truth)
    path = truth / name
    lines = rows(path)
    edit(lines)
    with path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(lines[0]), lineterminator="\n")
        writer.writeheader()
        writer.writerows(lines)
    return truth


def test_a_cent_off_in_the_key_exits_5_naming_the_number(warehouse_copy, tmp_path):
    def edit(lines):
        row = next(r for r in lines if r["customer_id"] == "acct_0001" and r["month"] == MONTH)
        row["ar_closing_cents"] = str(int(row["ar_closing_cents"]) + 1)

    truth = scratch_truth(tmp_path, "expected_rollforward.csv", edit)
    done = close(warehouse_copy, truth=truth)
    assert done.code == 5, done.stderr
    assert done.json["status"] == "failed"
    [failed] = done.json["failed_controls"]
    assert failed["id"] == "CHK-TRUTH-ROLLFORWARD" and failed["violating_rows"] == 1
    number = f"ar/{MONTH}/acct_0001/closing"
    assert failed["detail"]["number_ids"] == [number]
    [difference] = failed["detail"]["differences"]
    assert difference["number_id"] == number and difference["actual"] == difference["expected"] - 1
    assert number in done.json["message"]
    assert failed["remediation"]


def test_a_variant_moved_to_another_customer_in_the_key_exits_5(warehouse_copy, tmp_path):
    def edit(lines):
        row = next(r for r in lines if r["source_system"] == "bank_name")
        row["true_customer_id"] = "acct_9999"

    truth = scratch_truth(tmp_path, "identities.csv", edit)
    done = close(warehouse_copy, truth=truth)
    assert done.code == 5, done.stderr
    [failed] = done.json["failed_controls"]
    assert failed["id"] == "CHK-TRUTH-IDENTITIES"
    assert all(n.startswith("identity/") for n in failed["detail"]["number_ids"])


def test_a_number_without_a_provenance_record_exits_6(warehouse_copy):
    con = db.connect(warehouse_copy)
    try:
        con.execute(f"DELETE FROM prov.records WHERE number_id = 'ar/{MONTH}/acct_0001/closing'")
    finally:
        con.close()
    done = close(warehouse_copy)
    assert done.code == 6, done.stderr
    [failed] = done.json["failed_controls"]
    assert failed["id"] == "CHK-PROV-COVERAGE"
    assert failed["detail"]["number_ids"] == [f"ar/{MONTH}/acct_0001/closing"]


def test_check_without_the_answer_key_exits_4_and_builds_nothing(tmp_path):
    empty = tmp_path / "no-truth"
    empty.mkdir()
    done = close(tmp_path / "w.duckdb", truth=empty)
    assert done.code == 4
    assert "answer key" in done.json["message"]
    assert not (tmp_path / "w.duckdb").exists()
