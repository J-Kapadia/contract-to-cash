"""The health checks behind `c2c doctor`: each is a row `{check, status, detail}`.

A check never raises: a problem is its row's status, `warn` for something to look at and `fail`
for something that stops the product working.
"""

from __future__ import annotations

import importlib
import platform
import tomllib
from importlib import metadata
from pathlib import Path

import duckdb

from . import db, load, paths, policy, signoff, state
from .errors import C2CError

OK, WARN, FAIL = "ok", "warn", "fail"
EXIT = {OK: 0, WARN: 1, FAIL: 2}
LOCKFILE = Path(__file__).resolve().parents[2] / "uv.lock"
LOCKED_PACKAGES = ("dbt-core", "dbt-duckdb", "duckdb")
IMPORTS = (("dbt", "dbt.version"), ("duckdb", "duckdb"))


def row(check: str, status: str, detail: str) -> dict:
    return {"check": check, "status": status, "detail": detail}


def exit_code(rows: list[dict]) -> int:
    """0 when every check is ok, 1 when the worst is a warning, 2 when any failed."""
    return max((EXIT[r["status"]] for r in rows), default=0)


def locked_versions(lockfile: Path = LOCKFILE) -> dict[str, str]:
    """The version of each package in `uv.lock`; empty when the lock file is not there."""
    if not lockfile.is_file():
        return {}
    data = tomllib.loads(lockfile.read_text(encoding="utf-8"))
    return {p["name"]: p["version"] for p in data.get("package", []) if "version" in p}


def python_version() -> dict:
    return row("python", OK, f"{platform.python_version()} ({platform.python_implementation()})")


def versions(lockfile: Path = LOCKFILE) -> dict:
    """Installed versions of the locked packages against `uv.lock`."""
    locked = locked_versions(lockfile)
    installed, differing = {}, []
    for name in LOCKED_PACKAGES:
        try:
            installed[name] = metadata.version(name)
        except metadata.PackageNotFoundError:
            return row("versions", FAIL, f"{name} is not installed: run `uv sync --frozen`")
        if name in locked and locked[name] != installed[name]:
            differing.append(f"{name} {installed[name]} installed, {locked[name]} locked")
    summary = ", ".join(f"{name} {version}" for name, version in installed.items())
    if differing:
        return row("versions", WARN, f"{'; '.join(differing)}: run `uv sync --frozen`")
    return row("versions", OK, summary if locked else f"{summary} (no uv.lock to compare)")


def importable() -> list[dict]:
    rows = []
    for label, module in IMPORTS:
        try:
            importlib.import_module(module)
        except Exception as exc:
            rows.append(row(f"import {label}", FAIL, f"{type(exc).__name__}: {exc}"))
        else:
            rows.append(row(f"import {label}", OK, f"{module} imports"))
    return rows


def books(resolved: paths.Paths) -> dict:
    """The books' window and manifest."""
    try:
        first, last = load.window(resolved.books)
    except C2CError as exc:
        return row("books", WARN, exc.message)
    except (OSError, ValueError, KeyError) as exc:
        return row("books", FAIL, f"{resolved.books / 'window.json'} cannot be read: {exc}")
    identity = state.books_id(resolved.books)
    if identity is None:
        return row("books", WARN, f"{resolved.books} covers {first}..{last} but has no manifest beside it")
    return row("books", OK, f"{resolved.books} covers {first}..{last}, manifest {identity[:12]}")


def sign_off(resolved: paths.Paths) -> dict:
    try:
        record = signoff.check(resolved.config, resolved.signoff)
    except signoff.NotSigned:
        return row("signoff", WARN, f"the files in {resolved.config} are not signed: "
                                    "review them, then run `c2c signoff` in a terminal")
    except C2CError as exc:
        return row("signoff", FAIL, exc.message)
    return row("signoff", OK, f"signed by {record['signer']} ({record['role']}) on {record['signed_on']}")


def warehouse(resolved: paths.Paths) -> tuple[dict, duckdb.DuckDBPyConnection | None]:
    """The warehouse check and, when it opened, a read-only connection for the checks that need one."""
    if not resolved.warehouse.is_file():
        return row("warehouse", WARN, f"no warehouse at {resolved.warehouse}: close a month first"), None
    try:
        con = db.connect(resolved.warehouse, read_only=True)
        con.execute("SELECT count(*) FROM information_schema.tables").fetchone()
    except Exception as exc:
        return row("warehouse", FAIL, f"{resolved.warehouse} cannot be opened: {exc}"), None
    return row("warehouse", OK, f"{resolved.warehouse} opens"), con


def stale_reviews(resolved: paths.Paths, con: duckdb.DuckDBPyConnection | None) -> dict:
    """Reviewed pairs in the matches file that the warehouse's candidate pairs no longer contain."""
    check = "stale reviewed pairs"
    try:
        matches = policy.load_matches(resolved.config)
    except C2CError as exc:
        return row(check, FAIL, exc.message)
    if con is None:
        return row(check, OK, "skipped: no open warehouse")
    try:
        have = {frozenset(pair) for pair in con.execute(
            "SELECT source_a, source_b FROM mart.fct_match_candidates").fetchall()}
    except duckdb.Error:
        return row(check, OK, "skipped: no candidate pairs built yet")
    stale = [f"{m.a} / {m.b}" for m in matches if frozenset((m.a, m.b)) not in have]
    if stale:
        return row(check, WARN, f"{len(stale)} reviewed pair(s) match no candidate pair: {', '.join(stale)}")
    return row(check, OK, f"all {len(matches)} reviewed pair(s) are candidate pairs")


def lock_state(resolved: paths.Paths) -> dict:
    """Doctor runs under the shared lock, so reaching it means no writer holds the warehouse."""
    return row("lock", OK, f"no writer holds {resolved.warehouse}")


def truth(resolved: paths.Paths) -> dict:
    """The answer key is informational: it exists only where the books were generated."""
    where = "present" if resolved.truth.is_dir() else "absent"
    return row("truth", OK, f"{resolved.truth} {where} (informational)")


def run(resolved: paths.Paths) -> list[dict]:
    """Every check, in the order `doctor` prints them."""
    rows = [python_version(), versions(), *importable(), books(resolved), sign_off(resolved)]
    opened, con = warehouse(resolved)
    try:
        rows += [opened, stale_reviews(resolved, con), lock_state(resolved), truth(resolved)]
    finally:
        if con is not None:
            con.close()
    return rows
