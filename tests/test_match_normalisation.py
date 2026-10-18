"""Name normalisation and candidate blocking.

The `normalise_name()` macro is rendered through dbt and evaluated in a scratch DuckDB; the models
are built over the `mini` books in a temporary warehouse.
"""

from __future__ import annotations

import csv
import functools
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest
from hypothesis import HealthCheck, given, settings, strategies as st

from c2c import db, dbt_runner, load
from conftest import MINI_BOOKS, MINI_TRUTH, MINI_WINDOW, ROOT, shared_dir

REFERENCE = ROOT / "data" / "reference"
MACROS = ROOT / "dbt" / "macros"

# ------------------------------------------------------------------ the macro, evaluated

# The second application is a layer of its own: the macro reads a table, which DuckDB does not
# allow inside a lambda, so it cannot be nested in its own argument.
SCRATCH_MODEL = ("select name, once, {{ normalise_name('once') }} as twice from"
                 " (select name, {{ normalise_name('name') }} as once from raw.names)")
SCRATCH_SOURCES = ("version: 2\nsources:\n  - name: raw\n    schema: raw\n    tables:\n"
                   "      - name: ref_legal_suffixes\n")


@functools.cache
def compiled_macro() -> str:
    """The SQL of a scratch model that applies the macro once and twice, from one `dbt compile`
    on a project that shares the repository's macro directory."""
    root = Path(tempfile.mkdtemp(prefix="c2c-normalise-"))
    (root / "models").mkdir()
    (root / "models" / "names.sql").write_text(SCRATCH_MODEL + "\n")
    (root / "models" / "sources.yml").write_text(SCRATCH_SOURCES)
    (root / "dbt_project.yml").write_text(
        'name: scratch\nversion: "0.0.1"\nconfig-version: 2\nprofile: scratch\n'
        'model-paths: ["models"]\nmacro-paths: ["macros"]\n')
    (root / "profiles.yml").write_text(
        "scratch:\n  target: dev\n  outputs:\n    dev:\n      type: duckdb\n"
        f"      path: {root / 'scratch.duckdb'}\n      threads: 1\n")
    (root / "macros").symlink_to(MACROS, target_is_directory=True)
    env = {**os.environ, "DBT_SEND_ANONYMOUS_USAGE_STATS": "false"}
    proc = subprocess.run(
        [sys.executable, "-c", "from dbt.cli.main import cli; cli()", "compile", "--quiet",
         "--no-use-colors", "--project-dir", str(root), "--profiles-dir", str(root),
         "--target-path", str(root / "target"), "--log-path", str(root / "logs")],
        env=env, capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        raise RuntimeError("dbt compile failed:\n" + (proc.stdout + proc.stderr)[-2000:])
    return (root / "target" / "compiled" / "scratch" / "models" / "names.sql").read_text()


@functools.cache
def _connection():
    # compiled SQL names the source's database after the scratch project's file
    path = Path(tempfile.mkdtemp(prefix="c2c-normalise-db-")) / "scratch.duckdb"
    con = db.connect(path)
    con.execute("CREATE SCHEMA IF NOT EXISTS raw")
    load.load_reference(con, REFERENCE)
    return con


def normalised(names: list[str]) -> list[tuple[str | None, str | None, str | None]]:
    """`(name, normalised once, normalised twice)` for each name, in input order."""
    con = _connection()
    con.execute("CREATE OR REPLACE TABLE raw.names AS SELECT unnest(?::VARCHAR[]) AS name", [names])
    return con.execute(f"SELECT name, once, twice FROM ({compiled_macro()})").fetchall()


def once(name: str) -> str | None:
    return normalised([name])[0][1]


WORDS = ["Bramble", "Robotics", "ACME", "Ünï", "café", "Zoë", "x", "42", "O'Neil", "R&D", "Ångström"]
SUFFIXES = ["Inc", "INC.", "Ltd", "LLC", "GmbH", "S.A.", "Corp", "Limited", "plc", "(Pty)"]
PUNCTUATION = [" ", "  ", ", ", ". ", "-", "_", " & ", "\t", " ", "/"]
names = st.one_of(
    st.text(max_size=40),
    st.lists(st.sampled_from(WORDS + SUFFIXES + PUNCTUATION), max_size=8).map("".join),
)


@settings(max_examples=300, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(st.lists(names, min_size=1, max_size=25))
def test_applying_the_normalisation_twice_equals_once(batch):
    for name, first, second in normalised(batch):
        assert second == first, f"{name!r}: once {first!r}, twice {second!r}"


@pytest.mark.parametrize("raw, expected", [
    ("Bramble Robotics Inc.", "bramble robotics"),
    ("BRAMBLE  ROBOTICS, LTD", "bramble robotics"),
    ("Reed & Sons (Pty) Ltd", "reed sons"),
    ("Umber Publishing Corp.", "umber publishing"),
    ("Café Noir", "café noir"),
    ("Café Noir", "café noir"),  # decomposed and composed spellings meet
    ("  spaced \t out  ", "spaced out"),
    ("Inc", ""),
    ("", ""),
])
def test_normalised_names(raw, expected):
    assert once(raw) == expected


def test_a_null_name_stays_null():
    con = _connection()
    con.execute("CREATE OR REPLACE TABLE raw.names AS SELECT NULL::VARCHAR AS name")
    assert con.execute(f"SELECT once, twice FROM ({compiled_macro()})").fetchall() == [(None, None)]


# ------------------------------------------------------------------ the models over `mini`


def load_mini(warehouse: Path) -> None:
    con = db.connect(warehouse)
    try:
        db.init_schemas(con)
        load.load_months(con, MINI_BOOKS, *MINI_WINDOW, "test")
        load.load_reference(con, REFERENCE)
    finally:
        con.close()


def build_matches(warehouse: Path) -> None:
    dbt_runner.build(warehouse, select="+int_match_candidates")


@pytest.fixture(scope="session")
def mini_loaded(tmp_path_factory) -> Path:
    """The `mini` books loaded into a warehouse, before any model has run. Read-only: a test that
    changes it copies it first."""

    def build(directory: Path) -> None:
        load_mini(directory / "w.duckdb")

    return shared_dir(tmp_path_factory, "match-mini-loaded", build) / "w.duckdb"


@pytest.fixture(scope="session")
def mini_matches(tmp_path_factory, mini_loaded) -> Path:
    """A copy of the loaded `mini` warehouse with the matching models built. Read-only."""

    def build(directory: Path) -> None:
        shutil.copy(mini_loaded, directory / "w.duckdb")
        build_matches(directory / "w.duckdb")

    return shared_dir(tmp_path_factory, "match-mini-built", build) / "w.duckdb"


def query(warehouse: Path, sql: str, params=()):
    con = db.connect(warehouse, read_only=True)
    try:
        return con.execute(sql, params).fetchall()
    finally:
        con.close()


def designed_pairs() -> list[tuple[str, str, str]]:
    with (MINI_TRUTH / "matches_expected.csv").open(encoding="utf-8", newline="") as fh:
        return [(r["source_a"], r["source_b"], r["decision"]) for r in csv.DictReader(fh)]


def test_every_designed_pair_is_a_candidate(mini_matches):
    pairs = designed_pairs()
    assert pairs, "the answer key has no designed pairs"
    got = {frozenset(p) for p in query(mini_matches, "SELECT source_a, source_b FROM int.int_match_candidates")}
    for a, b, _ in pairs:
        assert frozenset((a, b)) in got, f"{a} and {b} are not a candidate pair"


def test_variants_cover_the_four_systems(mini_matches):
    got = dict(query(mini_matches, "SELECT source_system, count(*) FROM int.int_match_variants GROUP BY 1"))
    assert set(got) == {"crm", "billing", "bank_name", "gl_name"}
    assert got["crm"] == 10 and got["billing"] == 10


def test_a_free_mail_domain_is_ignored_and_a_website_is_a_domain(mini_matches):
    rows = query(mini_matches, "SELECT source_system, domain FROM int.int_match_variants"
                               " WHERE domain IS NOT NULL OR source_system IN ('crm', 'billing')")
    assert all(domain not in {"gmail.com", "yahoo.com", "outlook.com"} for _, domain in rows)
    assert ("crm", "bramblerobotics.example") in rows


def test_a_candidate_pair_is_ordered_unique_and_says_why(mini_matches):
    rows = query(mini_matches, "SELECT system_a, source_a, system_b, source_b, block_reason"
                               " FROM int.int_match_candidates")
    keys = [(sa, a, sb, b) for sa, a, sb, b, _ in rows]
    assert len(keys) == len(set(keys))
    assert all((sa, a) < (sb, b) for sa, a, sb, b in keys)
    assert not any(sa == sb == "crm" for sa, _, sb, _ in keys)
    assert {reason for *_, reason in rows} <= {
        "name_token", "domain", "contact_email", "contact_email,name_token", "domain,name_token",
        "contact_email,domain", "contact_email,domain,name_token"}
    by_pair = {frozenset((a, b)): reason for _, a, _, b, reason in rows}
    # the same customer under two spellings of its name, a shared contact and a shared website domain
    assert "name_token" in by_pair[frozenset(("acct_0001", "bill_0001"))]
    assert "contact_email" in by_pair[frozenset(("acct_0001", "bill_0001"))]


def shuffled_copy(source: Path, directory: Path, order: list[int], tables: tuple[str, ...]) -> Path:
    """A copy of the loaded warehouse in which each of `tables` holds its rows in a permuted order."""
    target = directory / "w.duckdb"
    shutil.copy(source, target)
    con = db.connect(target)
    try:
        for table in tables:
            rows = con.execute(f"SELECT * FROM raw.{table} ORDER BY ALL").fetchall()
            permuted = [rows[i % len(rows)] for i in sorted(range(len(rows)), key=lambda i: order[i % len(order)])]
            con.execute(f"DELETE FROM raw.{table}")
            marks = ", ".join("?" * len(rows[0]))
            con.executemany(f"INSERT INTO raw.{table} VALUES ({marks})", permuted)
    finally:
        con.close()
    return target


@settings(max_examples=3, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(order=st.permutations(range(10)))
def test_row_order_of_the_sources_does_not_change_the_result(order, mini_loaded, mini_matches, tmp_path_factory):
    directory = Path(tempfile.mkdtemp(prefix="shuffled-", dir=tmp_path_factory.getbasetemp()))
    warehouse = shuffled_copy(mini_loaded, directory, list(order), ("crm_accounts", "crm_contacts"))
    build_matches(warehouse)
    for table in ("int_match_variants", "int_match_candidates"):
        sql = f"SELECT * FROM int.{table} ORDER BY ALL"
        assert query(warehouse, sql) == query(mini_matches, sql), table
    shutil.rmtree(directory, ignore_errors=True)
