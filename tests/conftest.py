"""Shared helpers: run `c2c` as a subprocess against a temporary books/warehouse."""

from __future__ import annotations

import fcntl
import functools
import shutil
import json
import os
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

import duckdb
import pytest
from hypothesis import settings

# The example database would otherwise create an untracked .hypothesis/ in the checkout.
settings.register_profile("c2c", database=None)
settings.load_profile("c2c")

ROOT = Path(__file__).resolve().parents[1]

# The committed `mini` preset: its books, its answer key and the books' first and last month.
# Read-only for the tests; `mini_books_copy` gives one that may be changed.
MINI_BOOKS = ROOT / "tests" / "fixtures" / "books"
MINI_TRUTH = ROOT / "tests" / "fixtures" / "truth"
MINI_WINDOW = ("2024-10", "2026-03")

# The configuration the commands read when a test names none; it is signed into a scratch directory
# once per process. Point it at the reviewed fixture configuration once that exists.
DEFAULT_CONFIG = ROOT / "config"
SIGNER, ROLE, SIGNED_ON = "Test signer (synthetic)", "finance_lead", "2026-01-01"


# ------------------------------------------------------------------ capped DuckDB connections

_real_connect = duckdb.connect


@functools.wraps(_real_connect)
def connect(*args, **kwargs):
    """`duckdb.connect` with the warehouse caps: the same `SET memory_limit` and `SET threads` as
    `c2c.db.connect`, applied after opening because DuckDB refuses a second connection to the
    same file with a different `config=`. Signature and return value are unchanged."""
    from c2c import db

    con = _real_connect(*args, **kwargs)
    limit = db.memory_limit().replace("'", "''")
    con.execute(f"SET memory_limit = '{limit}'")
    con.execute(f"SET threads = {db.THREADS}")
    return con


@pytest.fixture(scope="session", autouse=True)
def _cap_duckdb_connections():
    """Every connection a test opens, directly or through library code, carries the caps; an
    uncapped one sizes itself to the whole machine."""
    duckdb.connect = connect
    try:
        yield
    finally:
        duckdb.connect = _real_connect


def shared_dir(tmp_path_factory, name: str, build) -> Path:
    """A directory `build(directory)` filled once for the whole run, however many xdist workers
    there are: the first caller builds it under a file lock, the others wait and reuse it. It is
    read-only for the tests; one that changes it takes a copy first."""
    base = tmp_path_factory.getbasetemp()
    if os.environ.get("PYTEST_XDIST_WORKER"):
        base = base.parent  # the temp root every worker of this run shares
    directory, done = base / f"shared-{name}", base / f"shared-{name}.done"
    with open(base / f"shared-{name}.lock", "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if not done.exists():
            directory.mkdir()
            build(directory)
            done.touch()
    return directory


# ------------------------------------------------------------------ running the CLI


@dataclass
class Result:
    code: int
    stdout: str
    stderr: str

    @property
    def json(self) -> dict:
        return json.loads(self.stdout)


# Where a `c2c` run that names no path of its own reads and writes: a directory private to the
# running test, never the checkout's `warehouse/` or `data/`, so parallel workers cannot collide on
# the default warehouse lock or on the default books. Session fixtures run before any test has set
# one, so they fall back to a directory private to the process.
_default_root: Path | None = None
_process_default_root: Path | None = None


def default_paths() -> dict[str, str]:
    global _process_default_root
    root = _default_root
    if root is None:
        if _process_default_root is None:
            _process_default_root = Path(tempfile.mkdtemp(prefix="c2c-defaults-"))
        root = _process_default_root
    # dbt's target and log directories are private to the process too: runs on one worker are
    # serial, so they reuse the parsed project, and runs on different workers never share a file.
    if _process_default_root is None:
        _process_default_root = Path(tempfile.mkdtemp(prefix="c2c-defaults-"))
    return {
        "C2C_WAREHOUSE": str(root / "warehouse" / "c2c.duckdb"),
        "C2C_BOOKS": str(root / "data" / "books"),
        "DBT_TARGET_PATH": str(_process_default_root / "dbt-target"),
        "DBT_LOG_PATH": str(_process_default_root / "dbt-logs"),
    }


@pytest.hookimpl(tryfirst=True)
def pytest_cmdline_main(config):
    """Run in parallel by default, whenever the xdist plugin is loaded (`-n 0` still runs serially,
    `-p no:xdist` and `--collect-only` are untouched). A crashing worker is never respawned: a
    respawn loop outlives its controller and grows without bound, so the run fails on the first
    crash instead. Set here rather than in `addopts`, which would break `-p no:xdist`."""
    opts = config.option
    if hasattr(opts, "numprocesses") and opts.numprocesses is None and not opts.collectonly \
            and "PYTEST_XDIST_WORKER" not in os.environ:
        opts.numprocesses = min(12, os.cpu_count() or 1)
        opts.dist = "load"
        opts.maxworkerrestart = 0


@pytest.fixture(autouse=True)
def _private_default_paths(tmp_path_factory):
    global _default_root
    _default_root = tmp_path_factory.mktemp("defaults")
    try:
        yield
    finally:
        _default_root = None


_process_signoff_dir: Path | None = None


def default_signoff_dir() -> Path:
    """A sign-off of `DEFAULT_CONFIG` in a directory private to this process, made on first use."""
    global _process_signoff_dir
    if _process_signoff_dir is None:
        from c2c import signoff

        _process_signoff_dir = Path(tempfile.mkdtemp(prefix="c2c-signoff-"))
        signoff.write_for(DEFAULT_CONFIG, _process_signoff_dir, SIGNER, ROLE, SIGNED_ON)
    return _process_signoff_dir


def signed_config(tmp_path: Path, source: Path = DEFAULT_CONFIG) -> tuple[Path, Path]:
    """Copy the configuration directory `source` under `tmp_path` and sign the copy.

    Returns `(config_dir, signoff_dir)`. A test edits the copy to make its sign-off stale."""
    from c2c import signoff

    config_dir, signoff_dir = tmp_path / "config", tmp_path / "signoff"
    shutil.copytree(source, config_dir, ignore=shutil.ignore_patterns("signoff"))
    signoff.write_for(config_dir, signoff_dir, SIGNER, ROLE, SIGNED_ON)
    return config_dir, signoff_dir


def c2c(*args: str, env: dict | None = None) -> Result:
    """Run `python -m c2c` with every path pointed at a per-test temporary root and no `C2C_*`
    variable inherited from the environment; `env` adds or overrides variables."""
    full_env = {k: v for k, v in os.environ.items() if not k.startswith("C2C_")}
    full_env.update(default_paths())
    full_env["C2C_SIGNOFF_DIR"] = str(default_signoff_dir())
    full_env.update(env or {})
    proc = subprocess.run(
        [sys.executable, "-m", "c2c", *args],
        cwd=ROOT, env=full_env, capture_output=True, text=True, check=False,
    )
    return Result(proc.returncode, proc.stdout, proc.stderr)


@dataclass
class Workspace:
    root: Path

    @property
    def books(self) -> Path:
        return self.root / "books"

    @property
    def warehouse(self) -> Path:
        return self.root / "w.duckdb"

    def flags(self) -> list[str]:
        return ["--books", str(self.books), "--warehouse", str(self.warehouse)]

    def run(self, *args: str, env: dict | None = None) -> Result:
        return c2c(*args, *self.flags(), env=env)


@pytest.fixture
def mini_books_copy(tmp_path) -> Path:
    """A private copy of the committed `mini` books, for a test that edits them: `books/` under a
    fresh directory, which is the parent of the returned path."""
    books = tmp_path / "books"
    shutil.copytree(MINI_BOOKS, books)
    return books


@pytest.fixture
def workspace(tmp_path) -> Workspace:
    """A private books directory and warehouse path for a test that runs commands."""
    return Workspace(tmp_path)


# ------------------------------------------------------------------ a closed month

CLOSED_MONTH = "2026-03"


@dataclass
class Closed:
    books: Path
    warehouse: Path


@pytest.fixture(scope="session")
def closed(tmp_path_factory) -> Closed:
    """The `small` books generated and 2026-03 closed once for the whole run, however many xdist
    workers there are. Read-only for the tests: one that changes the warehouse closes its own."""

    def build(root: Path) -> None:
        gen = c2c("gen", "--fixture", "small", "--out", str(root / "data"))
        assert gen.code == 0, gen.stderr
        books = root / "data" / "books" / "small"
        done = c2c("close", "--month", CLOSED_MONTH, "--books", str(books),
                   "--warehouse", str(root / "w.duckdb"), "--json")
        assert done.code == 0, done.stderr

    root = shared_dir(tmp_path_factory, "closed-small", build)
    return Closed(books=root / "data" / "books" / "small", warehouse=root / "w.duckdb")
