"""CTL-DRIFT-SCHEMA: each close records the columns of every books file, and the raw stage compares the
loaded files with the layout recorded at the latest month closed before the one being closed."""

from __future__ import annotations

import csv
import re
from pathlib import Path

import pytest

from c2c import db
from c2c.gen import BOOKS

from conftest import MINI_BOOKS, c2c

MONTH, BASELINE = "2024-11", "2024-10"
CONTROL = "CTL-DRIFT-SCHEMA"


def close(books: Path, warehouse: Path, trace: Path | None = None):
    env = {"C2C_BOOKS": ""}
    if trace is not None:
        env["C2C_DBT_TRACE"] = str(trace)
    return c2c("close", "--month", MONTH, "--fixture", "mini", "--books", str(books), "--warehouse", str(warehouse),
               "--json", env=env)


def header(books: Path, stem: str) -> str:
    with open(books / f"{stem}.csv", newline="", encoding="utf-8") as fh:
        return ",".join(next(csv.reader(fh)))


def recorded(warehouse: Path) -> dict[str, str]:
    with db.connect(warehouse, read_only=True) as con:
        return dict(con.execute("SELECT number_id, value_text FROM prov.records WHERE number_id LIKE 'schema/%'").fetchall())


@pytest.fixture(scope="module")
def injected(tmp_path_factory) -> Path:
    out = tmp_path_factory.mktemp("extra-column") / "books"
    done = c2c("inject", "--error", "extra_column", "--month", MONTH, "--books", str(MINI_BOOKS), "--out", str(out))
    assert done.code == 0, done.stderr
    return out


def widened(books: Path) -> str:
    (stem,) = [s for s in BOOKS if header(books, s) != header(MINI_BOOKS, s)]
    return stem


def test_a_clean_close_records_every_layout_and_a_second_close_passes(tmp_path):
    warehouse = tmp_path / "w.duckdb"
    first = close(MINI_BOOKS, warehouse)
    assert first.code == 0, first.stderr
    assert any(c["id"] == CONTROL and c["status"] == "pass" for c in first.json["controls"])

    expected = {f"schema/{month}/{stem}": header(MINI_BOOKS, stem) for month in (BASELINE, MONTH) for stem in BOOKS}
    assert len(BOOKS) == 16
    assert recorded(warehouse) == expected

    again = close(MINI_BOOKS, warehouse)
    assert again.code == 0, again.stderr


def test_an_added_column_fails_the_raw_stage_before_dbt_starts(injected, tmp_path):
    warehouse, trace = tmp_path / "w.duckdb", tmp_path / "dbt.trace"
    assert close(MINI_BOOKS, warehouse).code == 0
    stem = widened(injected)

    done = close(injected, warehouse, trace)

    assert done.code == 3, done.stderr
    assert not trace.exists()
    (failed,) = done.json["failed_controls"]
    assert failed["id"] == CONTROL and failed["violating_rows"] == 1
    (remediation,) = failed["remediation"]
    assert f"{stem}.csv" in remediation and "note" in remediation and BASELINE in remediation
    assert header(injected, stem) in remediation and header(MINI_BOOKS, stem) in remediation
    assert "{" not in remediation
    assert recorded(warehouse) == {f"schema/{m}/{s}": header(MINI_BOOKS, s) for m in (BASELINE, MONTH) for s in BOOKS}


def test_the_injected_copy_closes_on_a_fresh_warehouse(injected, tmp_path):
    done = close(injected, tmp_path / "w.duckdb")
    assert done.code == 0, done.stderr
    assert any(c["id"] == CONTROL and c["status"] == "pass" for c in done.json["controls"])
    assert re.search(r"note$", recorded(tmp_path / "w.duckdb")[f"schema/{MONTH}/{widened(injected)}"])
