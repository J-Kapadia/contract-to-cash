"""The `master/` and `rollup/` families: every row of `mart.fct_rollup` has exactly one provenance record
carrying the row's value, `explain` resolves one of each, and a month without rows registers nothing."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from c2c import db, prov

from conftest import MINI_BOOKS, MINI_WINDOW, c2c, shared_dir

MONTH = "2026-03"
EARLIER = "2025-12"


def command(*args: str, warehouse: Path):
    # the harness points C2C_BOOKS at a private root, which would win over the fixture's books
    return c2c(*args, "--fixture", "mini", "--books", str(MINI_BOOKS), "--warehouse", str(warehouse), "--json")


@pytest.fixture(scope="module")
def closed(tmp_path_factory) -> Path:
    """A warehouse where `master --check` ran for the month and the close followed it."""
    def build(directory: Path) -> None:
        warehouse = directory / "w.duckdb"
        built = command("master", "--month", MONTH, "--check", warehouse=warehouse)
        assert built.code == 0, built.stderr
        done = command("close", "--month", MONTH, warehouse=warehouse)
        assert done.code == 0, done.stderr

    return shared_dir(tmp_path_factory, "families-master-rollup", build) / "w.duckdb"


def rows(warehouse: Path, sql: str, params=()):
    with db.connect(warehouse, read_only=True) as con:
        return con.execute(sql, params).fetchall()


def number_id(month: str, metric: str) -> str:
    return metric if metric.startswith("master/") else f"rollup/{month}/{metric}"


def test_every_rollup_row_has_exactly_one_record_with_its_value(closed):
    # the snapshots reach back before the books' window, and no close covers those months
    table = rows(closed, "SELECT month, metric, value_cents, value_text FROM mart.fct_rollup WHERE month >= ?"
                 " ORDER BY ALL", [MINI_WINDOW[0]])
    assert {m for _, m, _, _ in table} >= {"stale_opps", "master/precision_auto", "master/recall_auto",
                                           "master/pairs_doubt", "master/pairs_reviewed"}
    for month, metric, cents, text in table:
        found = rows(closed, "SELECT value_cents, value_text, month FROM prov.records WHERE number_id = ?",
                     [number_id(month, metric)])
        assert found == [(cents, text, month)], (month, metric)
    families = rows(closed, "SELECT count(*) FROM prov.records WHERE split_part(number_id, '/', 1) IN ('master', 'rollup')")
    assert families == [(len(table),)]


@pytest.mark.parametrize("number", ["master/pairs_doubt", f"rollup/{MONTH}/stale_opps"])
def test_explain_resolves_a_master_and_a_rollup_id(closed, number):
    done = c2c("explain", number, "--books", str(MINI_BOOKS), "--warehouse", str(closed), "--json")
    assert done.code == 0, done.stderr
    chain = done.json["explanation"]
    assert chain["record"]["number_id"] == number and chain["digest_matches"] is True
    assert chain["derived_row_count"] > 0 and chain["sources"]


def test_a_month_without_rollup_rows_registers_nothing(closed, tmp_path):
    copy = tmp_path / "w.duckdb"
    shutil.copy(closed, copy)
    with db.connect(copy) as con:
        con.execute("DELETE FROM mart.fct_rollup WHERE month = ?", [EARLIER])
        prov.write_month(con, EARLIER)
        assert con.execute("SELECT count(*) FROM prov.records WHERE split_part(number_id, '/', 1) = 'rollup'"
                           " AND month = ?", [EARLIER]).fetchone() == (0,)
        assert con.execute("SELECT count(*) FROM prov.records WHERE number_id = 'master/pairs_doubt'").fetchone() == (1,)


def test_a_close_with_no_master_rows_still_passes(tmp_path):
    warehouse = tmp_path / "w.duckdb"
    done = command("close", "--month", MONTH, warehouse=warehouse)
    assert done.code == 0, done.stderr
    assert rows(warehouse, "SELECT count(*) FROM prov.records WHERE split_part(number_id, '/', 1) = 'master'") == [(0,)]
    assert rows(warehouse, "SELECT count(*) FROM prov.records WHERE number_id = ?", [f"rollup/{MONTH}/stale_opps"]) == [(1,)]
