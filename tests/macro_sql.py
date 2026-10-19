"""Render the warehouse macros through dbt and evaluate the SQL in a scratch DuckDB.

`dbt compile` runs once per process on a scratch project that shares the repository's macro
directory, so the SQL under test is exactly the SQL a model gets. The compiled expressions are
then evaluated over any number of rows in a temporary DuckDB opened through `c2c.db`.
"""

from __future__ import annotations

import functools
import os
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Iterable, Sequence
from datetime import date
from pathlib import Path
from typing import Any

from c2c import db

# Directories this process created; the session-finish hook in conftest.py removes them.
created_dirs: list[Path] = []

MACROS = Path(__file__).resolve().parents[1] / "dbt" / "macros"

# One scratch model per macro; the macro arguments are the columns of the `c2c_in` table.
MODELS = {
    "ratable": "select rowid, {{ ratable_cents('amount_cents', 'service_start', 'service_end', 'month') }}"
               " as cents from c2c_in",
    "split": "select rowid, {{ split_exact('total_cents', 'weight_bps', 'part_key', 'part_group') }}"
             " as cents from c2c_in",
}

INPUT_TABLES = {
    "ratable": "amount_cents BIGINT, service_start DATE, service_end DATE, month VARCHAR",
    "split": "total_cents BIGINT, weight_bps BIGINT, part_key VARCHAR, part_group INTEGER",
}


def _scratch_dir(prefix: str) -> Path:
    path = Path(tempfile.mkdtemp(prefix=prefix))
    created_dirs.append(path)
    return path


def remove_created() -> None:
    """Remove every scratch directory this process created."""
    while created_dirs:
        shutil.rmtree(created_dirs.pop(), ignore_errors=True)


def _write_project(root: Path) -> None:
    (root / "models").mkdir()
    (root / "dbt_project.yml").write_text(
        'name: scratch\nversion: "0.0.1"\nconfig-version: 2\nprofile: scratch\n'
        'model-paths: ["models"]\nmacro-paths: ["macros"]\n')
    (root / "profiles.yml").write_text(
        "scratch:\n  target: dev\n  outputs:\n    dev:\n      type: duckdb\n"
        f"      path: {root / 'scratch.duckdb'}\n      threads: 1\n")
    (root / "macros").symlink_to(MACROS, target_is_directory=True)
    for name, sql in MODELS.items():
        (root / "models" / f"{name}.sql").write_text(sql + "\n")


@functools.cache
def compiled() -> dict[str, str]:
    """The compiled SQL of each scratch model, from one `dbt compile`."""
    root = _scratch_dir("c2c-macros-")
    _write_project(root)
    env = {**os.environ, "DBT_SEND_ANONYMOUS_USAGE_STATS": "false"}
    proc = subprocess.run(
        [sys.executable, "-c", "from dbt.cli.main import cli; cli()", "compile", "--quiet",
         "--no-use-colors", "--project-dir", str(root), "--profiles-dir", str(root),
         "--target-path", str(root / "target"), "--log-path", str(root / "logs")],
        env=env, capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        raise RuntimeError("dbt compile failed:\n" + (proc.stdout + proc.stderr)[-2000:])
    out = {name: (root / "target" / "compiled" / "scratch" / "models" / f"{name}.sql").read_text()
           for name in MODELS}
    return out


@functools.cache
def _connection() -> Any:
    path = _scratch_dir("c2c-macro-db-") / "macros.duckdb"
    return db.connect(path)


def _literal(value: Any) -> str:
    if isinstance(value, date):
        return f"DATE '{value.isoformat()}'"
    if isinstance(value, str):
        return "'" + value.replace("'", "''") + "'"
    return str(int(value))


def _evaluate(name: str, rows: Sequence[Sequence[Any]]) -> list[int | None]:
    """The macro's value for each input row, in input order."""
    con = _connection()
    con.execute("DROP TABLE IF EXISTS c2c_in")
    con.execute(f"CREATE TABLE c2c_in ({INPUT_TABLES[name]})")
    if rows:  # one literal INSERT: executemany is orders of magnitude slower
        values = ", ".join("(" + ", ".join(_literal(v) for v in row) + ")" for row in rows)
        con.execute(f"INSERT INTO c2c_in VALUES {values}")
    result = con.execute(f"SELECT cents FROM ({compiled()[name]}) ORDER BY rowid").fetchall()
    return [r[0] for r in result]


def ratable_cents(rows: Iterable[tuple[int, date, date, str]]) -> list[int | None]:
    """`ratable_cents(amount, start, end, month)` for each `(amount, start, end, 'YYYY-MM')` row."""
    return _evaluate("ratable", list(rows))


def split_exact(rows: Iterable[tuple[int, int, str, int]]) -> list[int | None]:
    """`split_exact(total, weight_bps, key, group)` for each row, partitioned by the group."""
    return _evaluate("split", list(rows))
