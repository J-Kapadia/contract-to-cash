"""Every DuckDB connection the product opens is memory-capped, and dbt's connection too.

DuckDB's default is 80% of the machine's memory per connection; a command, its dbt subprocess
and tests running in parallel each open one, so every connection takes the cap in
C2C_DUCKDB_MEMORY_LIMIT (default 2GB), one thread, and spills beside the warehouse file.
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

import jinja2
import pytest
from dbt.clients.yaml_helper import load_yaml_text

from c2c import db
from conftest import ROOT, c2c

VAR = "C2C_DUCKDB_MEMORY_LIMIT"
UNITS = {
    "B": 1, "bytes": 1,
    "KiB": 1024, "MiB": 1024**2, "GiB": 1024**3, "TiB": 1024**4,
    "KB": 1000, "MB": 1000**2, "GB": 1000**3, "TB": 1000**4,
}


def as_bytes(setting: str) -> float:
    """DuckDB reports a limit in binary units, rounded: `1.8 GiB`."""
    m = re.fullmatch(r"([0-9.]+)\s*([A-Za-z]+)", setting.strip())
    assert m, setting
    return float(m.group(1)) * UNITS[m.group(2)]


def settings(con) -> tuple[str, str, int]:
    return con.execute(
        "SELECT current_setting('memory_limit'), current_setting('temp_directory'),"
        " current_setting('threads')"
    ).fetchone()


def test_connection_takes_the_default_cap_and_spills_beside_the_file(tmp_path, monkeypatch):
    monkeypatch.delenv(VAR, raising=False)
    with db.connect(tmp_path / "w.duckdb") as con:
        limit, temp, threads = settings(con)
    assert abs(as_bytes(limit) - 2 * 1000**3) < 0.05 * 2 * 1000**3, limit
    assert Path(temp) == tmp_path / "w.duckdb.tmp"
    assert threads == 1


def test_env_var_overrides_the_cap(tmp_path, monkeypatch):
    monkeypatch.setenv(VAR, "512MiB")
    with db.connect(tmp_path / "w.duckdb") as con:
        assert settings(con)[0] == "512.0 MiB"


def test_read_only_connection_is_capped(tmp_path, monkeypatch):
    monkeypatch.setenv(VAR, "512MiB")
    with db.connect(tmp_path / "w.duckdb") as con:
        con.execute("CREATE TABLE t (x INTEGER)")
    with db.connect(tmp_path / "w.duckdb", read_only=True) as con:
        limit, temp, threads = settings(con)
    assert limit == "512.0 MiB"
    assert Path(temp) == tmp_path / "w.duckdb.tmp"
    assert threads == 1


def test_init_schemas_creates_every_schema(tmp_path):
    with db.connect(tmp_path / "w.duckdb") as con:
        db.init_schemas(con)
        db.init_schemas(con)  # idempotent
        names = {r[0] for r in con.execute("SELECT schema_name FROM information_schema.schemata").fetchall()}
    assert set(db.SCHEMAS) == {"raw", "mart", "ctl", "prov", "state", "forecast"}
    assert set(db.SCHEMAS) <= names


def test_dbt_profile_reads_the_same_cap(monkeypatch):
    """The profile dbt is given (`--profiles-dir dbt`) sets DuckDB's memory_limit from the same
    variable with the same default, and runs one thread."""
    raw = load_yaml_text((ROOT / "dbt" / "profiles.yml").read_text())
    output = raw["c2c"]["outputs"]["dev"]
    assert raw["c2c"]["target"] == "dev"
    assert output["type"] == "duckdb"
    assert output["threads"] == 1
    template = output["settings"]["memory_limit"]
    assert VAR in template

    def render() -> str:
        return jinja2.Template(template).render(env_var=lambda k, d=None: os.environ.get(k, d))

    monkeypatch.delenv(VAR, raising=False)
    assert render() == db.DEFAULT_MEMORY_LIMIT
    monkeypatch.setenv(VAR, "512MiB")
    assert render() == "512MiB"


def test_test_connections_are_capped_too(tmp_path, monkeypatch):
    """A connection a test opens directly, not through `c2c.db`, carries the cap."""
    import duckdb

    open_connection = duckdb.connect  # the patched attribute, not a literal call site
    monkeypatch.setenv(VAR, "512MiB")
    con = open_connection(str(tmp_path / "w.duckdb"))
    try:
        limit, _, threads = settings(con)
    finally:
        con.close()
    assert limit == "512.0 MiB"
    assert threads == 1


def test_c2c_helper_isolates_the_run_from_the_environment(monkeypatch):
    """The helper strips inherited `C2C_*` variables and points every path at a private root."""
    monkeypatch.setenv("C2C_BOOKS", "/nonexistent/books")
    monkeypatch.setenv("C2C_TRUTH", "/nonexistent/truth")
    result = c2c("--version")
    assert result.code == 0, result.stderr
    assert result.stdout.startswith("c2c, version")


def test_only_the_helper_and_the_conftest_wrapper_open_connections():
    """A guard on the call itself, so no command can open an uncapped connection."""
    pattern = "duckdb" + r"\.connect\("
    out = subprocess.run(
        ["grep", "-rnE", "--include=*.py", pattern, "src", "tests"],
        cwd=ROOT, capture_output=True, text=True, check=False,
    ).stdout.splitlines()
    files = {line.split(":", 1)[0] for line in out}
    assert files <= {"src/c2c/db.py", "tests/conftest.py"}, out
    assert len([line for line in out if line.startswith("src/")]) == 1, out
