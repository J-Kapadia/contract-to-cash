"""Every control file opens with its remediation template, and every `{placeholder}` of the template is a
column the control's query returns, checked on a warehouse with the models built and no rows in them."""

from __future__ import annotations

import re
import shutil
from pathlib import Path

import pytest

from c2c import controls, db, dbt_runner, load, prov, state
from conftest import DEFAULT_CONFIG, MINI_BOOKS, MINI_WINDOW, ROOT, shared_dir

REFERENCE = ROOT / "data" / "reference"
CONTROL_FILES = sorted(controls.CONTROLS_DIR.glob("CTL-*.sql"))
PRIVATE_TERMS = [line for line in (ROOT / "scripts" / "private-terms.txt").read_text(encoding="utf-8").splitlines()
                 if line.strip() and not line.startswith("#")]
MONTH = "2026-01"


def build_empty(directory: Path) -> None:
    """The `mini` books with every table cut to its header, loaded and built through every model."""
    books = directory / "books"
    shutil.copytree(MINI_BOOKS, books)
    for table in books.glob("*.csv"):
        table.write_text(table.read_text(encoding="utf-8").split("\n", 1)[0] + "\n", encoding="utf-8")
    warehouse = directory / "w.duckdb"
    con = db.connect(warehouse)
    try:
        db.init_schemas(con)
        # a control may read the month state and the recorded numbers, both empty here
        state.init(con)
        prov.init(con)
        load.load_months(con, books, *MINI_WINDOW, "test")
        load.load_reference(con, REFERENCE)
        load.load_config(con, DEFAULT_CONFIG)
    finally:
        con.close()
    dbt_runner.build(warehouse)


@pytest.fixture(scope="module")
def empty_warehouse(tmp_path_factory) -> Path:
    return shared_dir(tmp_path_factory, "empty-warehouse", build_empty) / "w.duckdb"


def test_there_is_a_control_for_every_check():
    assert CONTROL_FILES, "no controls found"


@pytest.mark.parametrize("path", CONTROL_FILES, ids=lambda p: p.stem)
def test_line_one_is_the_remediation_template(path):
    first = path.read_text(encoding="utf-8").split("\n", 1)[0]
    assert re.fullmatch(r"-- remediation: \S.*\.", first), f"{path.name}: line 1 is not a remediation sentence"
    template, stage = controls._header(path)
    assert controls.PLACEHOLDER.findall(template), f"{path.name}: the remediation names no row"
    assert stage in {"raw", "mart"}


@pytest.mark.parametrize("path", CONTROL_FILES, ids=lambda p: p.stem)
def test_every_placeholder_is_a_returned_column(path, empty_warehouse):
    template, _ = controls._header(path)
    sql = path.read_text(encoding="utf-8").rstrip().rstrip(";")
    with db.connect(empty_warehouse, read_only=True) as con:
        cursor = con.execute(f"SELECT * FROM ({sql}) AS violations LIMIT 0", {"month": MONTH} if "$month" in sql else {})
        columns = [column[0] for column in cursor.description]
    missing = sorted(set(controls.PLACEHOLDER.findall(template)) - set(columns))
    assert not missing, f"{path.name}: {missing} are not returned columns ({columns})"


@pytest.mark.parametrize("path", CONTROL_FILES, ids=lambda p: p.stem)
def test_the_template_names_no_private_material(path):
    template, _ = controls._header(path)
    found = [term for term in PRIVATE_TERMS if re.search(term, template)]
    assert not found, f"{path.name}: the remediation matches {found}"
