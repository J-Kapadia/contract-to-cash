"""CTL-DRIFT-COUNTERPARTY: a bank counterparty the books never named before the latest month closed
before it stops the close. The clean `mini` passes in every month; a receipt paid under a new name fails
the month it lands in, until the name is listed in the counterparties reference file."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from c2c import db

from conftest import MINI_BOOKS, MINI_WINDOW, ROOT, c2c, shared_dir

CONTROL = "CTL-DRIFT-COUNTERPARTY"
MONTH, BASELINE = "2025-02", "2025-01"  # the first receipts of mini are in 2024-12; 2025-02 has three
UNMATCHED, CASH_APP = "CTL-BANK-UNMATCHED", "CTL-CASH-APP"
REFERENCE_FILE = "data/reference/counterparties.csv"


def months(first: str, last: str) -> list[str]:
    year, month = map(int, first.split("-"))
    out = []
    while f"{year}-{month:02d}" <= last:
        out.append(f"{year}-{month:02d}")
        year, month = (year + 1, 1) if month == 12 else (year, month + 1)
    return out


def close(books: Path, warehouse: Path, month: str, reference: Path | None = None):
    args = ["close", "--month", month, "--fixture", "mini", "--books", str(books), "--warehouse", str(warehouse), "--json"]
    if reference is not None:
        args += ["--reference", str(reference)]
    return c2c(*args, env={"C2C_BOOKS": ""})


@pytest.fixture(scope="module")
def closed_mini(tmp_path_factory) -> Path:
    def build(directory: Path) -> None:
        done = close(MINI_BOOKS, directory / "w.duckdb", MINI_WINDOW[1])
        assert done.code == 0, done.stderr

    return shared_dir(tmp_path_factory, "drift-counterparty-mini", build) / "w.duckdb"


@pytest.fixture(scope="module")
def injected(tmp_path_factory) -> Path:
    out = tmp_path_factory.mktemp("new-counterparty") / "books"
    done = c2c("inject", "--error", "new_counterparty", "--month", MONTH, "--books", str(MINI_BOOKS), "--out", str(out))
    assert done.code == 0, done.stderr
    return out


def new_text(books: Path) -> str:
    before = {l.split(",")[0]: l for l in (MINI_BOOKS / "bank_transactions.csv").read_text(encoding="utf-8").splitlines()}
    (changed,) = [l for l in (books / "bank_transactions.csv").read_text(encoding="utf-8").splitlines()
                  if before.get(l.split(",")[0]) != l]
    return changed.split(",")[3]


def test_a_clean_mini_has_a_pass_row_for_every_month(closed_mini):
    with db.connect(closed_mini, read_only=True) as con:
        rows = con.execute("SELECT month, status, violating_rows FROM ctl.control_results WHERE control_id = ? "
                           "ORDER BY month", [CONTROL]).fetchall()
    assert [(m, s, n) for m, s, n in rows] == [(m, "pass", 0) for m in months(*MINI_WINDOW)]


def test_a_receipt_under_a_new_name_stops_the_close(injected, tmp_path):
    text = new_text(injected)
    done = close(injected, tmp_path / "w.duckdb", MONTH)

    assert done.code == 3, done.stderr
    failed = {c["id"]: c for c in done.json["failed_controls"]}
    assert failed[CONTROL]["violating_rows"] == 1
    (remediation,) = failed[CONTROL]["remediation"]
    assert text in remediation and BASELINE in remediation and REFERENCE_FILE in remediation
    assert "{" not in remediation
    assert len(failed[CONTROL]["detail"]["row_hashes"]) == 1
    assert UNMATCHED not in failed and CASH_APP not in failed


def test_listing_the_name_in_the_reference_file_passes(injected, tmp_path):
    reference = tmp_path / "reference"
    shutil.copytree(ROOT / "data" / "reference", reference)
    with open(reference / "counterparties.csv", "a", encoding="utf-8", newline="") as fh:
        fh.write(f"{new_text(injected)},vendor,listed by the test\n")

    done = close(injected, tmp_path / "w.duckdb", MONTH, reference)

    assert done.code == 0, done.stderr
    assert any(c["id"] == CONTROL and c["status"] == "pass" for c in done.json["controls"])


def test_the_first_month_closed_has_no_baseline(injected, tmp_path):
    done = close(injected, tmp_path / "w.duckdb", MINI_WINDOW[0])
    assert done.code == 0, done.stderr
    assert any(c["id"] == CONTROL and c["status"] == "pass" for c in done.json["controls"])
