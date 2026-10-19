"""The general-ledger tie-out over the `mini` books: every tied account ties on clean books, and a
one-cent slip in a receivable ledger line fails the receivables tie-out and no other control."""

from __future__ import annotations

from pathlib import Path

import pytest

from c2c import db
from conftest import MINI_BOOKS, c2c, shared_dir

MONTH = "2026-03"
ACCOUNTS = ["1000", "1200", "2400", "4000", "4100"]


def close(books: Path, warehouse: Path):
    # the harness points C2C_BOOKS at a private root, which would win over the fixture's books
    return c2c("close", "--month", MONTH, "--fixture", "mini", "--books", str(books),
               "--warehouse", str(warehouse), "--json")


def query(warehouse: Path, sql: str, params=()):
    con = db.connect(warehouse, read_only=True)
    try:
        return con.execute(sql, params).fetchall()
    finally:
        con.close()


@pytest.fixture(scope="module")
def clean_warehouse(tmp_path_factory) -> Path:
    def build(directory: Path) -> None:
        done = close(MINI_BOOKS, directory / "w.duckdb")
        assert done.code == 0, done.stderr

    return shared_dir(tmp_path_factory, "tieout-mini", build) / "w.duckdb"


def test_every_month_and_account_ties_on_clean_books(clean_warehouse):
    assert query(clean_warehouse, "SELECT count(*) FROM mart.fct_gl_tieout WHERE diff_cents != 0") == [(0,)]
    accounts = query(clean_warehouse, "SELECT DISTINCT gl_account FROM mart.fct_gl_tieout ORDER BY 1")
    assert [a for (a,) in accounts] == ACCOUNTS
    months = query(clean_warehouse, "SELECT count(DISTINCT month) FROM mart.fct_gl_tieout")[0][0]
    rows = query(clean_warehouse, "SELECT count(*) FROM mart.fct_gl_tieout")[0][0]
    assert months >= 18 and rows == months * len(ACCOUNTS)  # one row per month and account


def test_the_ledger_side_is_not_trivially_zero(clean_warehouse):
    got = dict(query(clean_warehouse, "SELECT gl_account, gl_cents FROM mart.fct_gl_tieout WHERE month = ?",
                     [MONTH]))
    assert all(got[account] != 0 for account in ACCOUNTS)


def test_a_rounding_slip_fails_the_receivables_tie_only(tmp_path):
    injected = tmp_path / "injected"
    done = c2c("inject", "--error", "rounding_slip", "--fixture", "mini", "--out", str(injected), "--json",
               env={"C2C_BOOKS": ""})
    assert done.code == 0, done.stderr
    result = close(injected, tmp_path / "w.duckdb")
    assert result.code == 3, result.stderr
    failed = result.json["failed_controls"]
    assert [c["id"] for c in failed] == ["CTL-TIE-AR"]
    assert failed[0]["detail"] and any("1 cents" in text for text in failed[0]["remediation"])
    # the slip persists in the running balance, and only in account 1200
    slipped = query(tmp_path / "w.duckdb",
                    "SELECT DISTINCT gl_account, diff_cents FROM mart.fct_gl_tieout WHERE diff_cents != 0")
    assert slipped == [("1200", 1)]
