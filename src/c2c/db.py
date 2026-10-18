"""The one place a DuckDB connection is opened.

DuckDB takes 80% of the machine's memory and every core per connection, and several
processes open connections at once (a command, its dbt subprocess, tests running in
parallel). Every connection the product opens is capped here instead.
"""

from __future__ import annotations

import os
from pathlib import Path

import duckdb

SCHEMAS = ("raw", "mart", "ctl", "prov", "state", "forecast")

MEMORY_LIMIT_VAR = "C2C_DUCKDB_MEMORY_LIMIT"
DEFAULT_MEMORY_LIMIT = "2GB"
THREADS = 1


def memory_limit() -> str:
    """The cap on every connection: `C2C_DUCKDB_MEMORY_LIMIT`, default 2GB."""
    return os.environ.get(MEMORY_LIMIT_VAR) or DEFAULT_MEMORY_LIMIT


def temp_directory(path: Path) -> Path:
    """Where a connection spills to disk: beside the warehouse file."""
    return path.parent / f"{path.name}.tmp"


def _quoted(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def connect(path: str | Path, read_only: bool = False) -> duckdb.DuckDBPyConnection:
    """Open the warehouse at `path` with the caps applied.

    The caps are set with SET after opening rather than passed as connection config: DuckDB
    shares one instance per file within a process and refuses a second connection opened with
    a different config."""
    path = Path(path)
    if not read_only:
        path.parent.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect(str(path), read_only=read_only)
    con.execute(f"SET memory_limit = {_quoted(memory_limit())}")
    con.execute(f"SET threads = {THREADS}")
    con.execute(f"SET temp_directory = {_quoted(str(temp_directory(path)))}")
    return con


def init_schemas(con: duckdb.DuckDBPyConnection) -> None:
    """Create the warehouse schemas that are not dbt's own."""
    for schema in SCHEMAS:
        con.execute(f"CREATE SCHEMA IF NOT EXISTS {schema}")
