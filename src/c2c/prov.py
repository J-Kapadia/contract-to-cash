"""Provenance: one record per published number, with a digest of the inputs it came from.

Records are written only here. A number belongs to a family, declared as one SQL file under
`src/c2c/families/`, so a new family is a new file and no change to this module. The file's name is
its family and, by convention, the first segment of its number ids.

A family file is a SELECT, run with `$month` bound when it mentions it, returning one row per number:

    number_id     VARCHAR    stable id, e.g. `ar/2026-03/acct_0001/closing`
    month         VARCHAR    the month it belongs to, NULL for a number that belongs to none
    value_cents   BIGINT     the value when it is an integer (cents, a count, a rate in bps)
    value_text    VARCHAR    the value when it is text
    unit          VARCHAR    `cents`, `count`, `bps`, `text`, ...
    kind          VARCHAR    `leaf` or `composite`; anything else fails the write
    source_query  VARCHAR    leaves: a query selecting `_row_hash`, the source rows of the number
    children      VARCHAR[]  composites: the ids of the numbers it is computed from
    step          VARCHAR    optional: the model or file it comes from; the family name when absent

A file that names a parameter other than `$month` belongs to a step that supplies it (a forecast, say)
and is run there, not by `write_month`.

A leaf's digest is over the `_row_hash`es its `source_query` selects. A composite's is over the
`(number_id, input_digest)` pairs of its children in id order, so it changes when any child's does.
A composite's children may belong to any family, or be records already written.
"""

from __future__ import annotations

import functools
import hashlib
import json
import os
import re
import subprocess
import uuid
from collections.abc import Sequence
from pathlib import Path

import duckdb

UNKNOWN_SHA = "unknown"


def init(con: duckdb.DuckDBPyConnection) -> None:
    con.execute("CREATE SCHEMA IF NOT EXISTS prov")
    con.execute(
        "CREATE TABLE IF NOT EXISTS prov.records ("
        " number_id VARCHAR PRIMARY KEY, month VARCHAR, value_cents BIGINT, value_text VARCHAR, unit VARCHAR,"
        " step VARCHAR, git_sha VARCHAR, producer_kind VARCHAR, input_digest VARCHAR, input_row_count BIGINT,"
        " source_query VARCHAR, checks_passed VARCHAR[], signer VARCHAR, signoff_ref VARCHAR, run_id VARCHAR)"
    )


@functools.cache
def _head_sha() -> str:
    try:
        out = subprocess.run(["git", "rev-parse", "HEAD"], cwd=Path(__file__).resolve().parent,
                             capture_output=True, text=True, check=False)
    except OSError:
        return UNKNOWN_SHA
    return out.stdout.strip() if out.returncode == 0 and out.stdout.strip() else UNKNOWN_SHA


def git_sha() -> str:
    """The commit that produced a number: `C2C_GIT_SHA`, else `git rev-parse HEAD`, else "unknown"."""
    return os.environ.get("C2C_GIT_SHA") or _head_sha()


def producer_kind() -> str:
    return os.environ.get("C2C_PRODUCER_KIND") or "cli"


def digest(row_hashes: Sequence[str]) -> str:
    """SHA-256 over the sorted row hashes, one per line."""
    return hashlib.sha256("\n".join(sorted(row_hashes)).encode("utf-8")).hexdigest()


def source_digest(con: duckdb.DuckDBPyConnection, source_query: str) -> tuple[str, int]:
    """The input digest and row count of what `source_query` returns (it selects `_row_hash`)."""
    hashes = [row[0] for row in con.execute(source_query).fetchall()]
    return digest(hashes), len(hashes)


def write_leaf(con: duckdb.DuckDBPyConnection, number_id: str, month: str, value: int | str | None, unit: str,
               step: str, source_query: str, checks_passed: Sequence[str] = (), run_id: str | None = None) -> None:
    """Write (or replace) the record of one leaf number. An integer `value` is stored as cents."""
    init(con)
    input_digest, count = source_digest(con, source_query)
    is_text = isinstance(value, str)
    con.execute(
        "INSERT OR REPLACE INTO prov.records VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, ?)",
        [number_id, month, None if is_text else value, value if is_text else None, unit, step, git_sha(),
         producer_kind(), input_digest, count, source_query, list(checks_passed), run_id or uuid.uuid4().hex],
    )


FAMILIES_DIR = Path(__file__).resolve().parent / "families"
LEAF, COMPOSITE = "leaf", "composite"
COLUMNS = ("number_id", "month", "value_cents", "value_text", "unit", "kind", "source_query", "children")
# The families whose records make up a month's result; each contributes the ids `<family>/<month>/...`.
RECORD_TYPES = {
    "number_id": "VARCHAR", "month": "VARCHAR", "value_cents": "BIGINT", "value_text": "VARCHAR", "unit": "VARCHAR",
    "step": "VARCHAR", "git_sha": "VARCHAR", "producer_kind": "VARCHAR", "input_digest": "VARCHAR",
    "input_row_count": "BIGINT", "source_query": "VARCHAR", "checks_passed": "VARCHAR[]", "signer": "VARCHAR",
    "signoff_ref": "VARCHAR", "run_id": "VARCHAR",
}
RECORD_STRUCT = "{" + ", ".join(f'"{c}": "{t}"' for c, t in RECORD_TYPES.items()) + "}"
RESULT_FAMILIES = ("cust", "rev", "defrev", "ar", "cash", "bridge", "tie", "aging", "dtp", "rollup")


# `$name`, but not the `$$` that quotes a string in the file.
PARAMETER = re.compile(r"(?<!\$)\$([A-Za-z_]\w*)")


class FamilyError(ValueError):
    """A family file returned something that cannot be recorded."""


def month_families(directory: Path | None = None) -> list[Path]:
    """The family files `write_month` runs, in name order: those that need no parameter but `$month`."""
    return [p for p in sorted((directory or FAMILIES_DIR).glob("*.sql"))
            if set(PARAMETER.findall(p.read_text(encoding="utf-8"))) <= {"month"}]


def pair_digest(pairs: Sequence[tuple[str, str]]) -> str:
    """SHA-256 over `(number_id, input_digest)` pairs in id order, one per line."""
    return hashlib.sha256("\n".join(f"{n}\x1f{d}" for n, d in sorted(pairs)).encode("utf-8")).hexdigest()


def family_rows(con: duckdb.DuckDBPyConnection, path: Path, month: str) -> list[dict]:
    sql = path.read_text(encoding="utf-8")
    cursor = con.execute(sql, {"month": month} if "$month" in sql else {})
    names = [d[0] for d in cursor.description]
    missing = [c for c in COLUMNS if c not in names]
    if missing:
        raise FamilyError(f"family {path.stem!r} does not return {', '.join(missing)}")
    rows = [dict(zip(names, row)) for row in cursor.fetchall()]
    for row in rows:
        kind, number_id = row["kind"], row["number_id"]
        if kind == LEAF and not row["source_query"]:
            raise FamilyError(f"family {path.stem!r}: leaf {number_id!r} has no source_query")
        if kind == COMPOSITE and row["children"] is None:
            raise FamilyError(f"family {path.stem!r}: composite {number_id!r} has no children")
        if kind not in (LEAF, COMPOSITE):
            raise FamilyError(f"family {path.stem!r}: {number_id!r} has unknown kind {kind!r}; use leaf or composite")
        row["step"] = row.get("step") or path.stem
    return rows


def _digests(con: duckdb.DuckDBPyConnection, rows: dict[str, dict]) -> dict[str, tuple[str, int]]:
    """The `(input_digest, input_row_count)` of every row; a composite's children are looked up among
    the rows, then among the records already written."""
    done: dict[str, tuple[str, int]] = {}

    def of(number_id: str, trail: tuple[str, ...] = ()) -> str:
        if number_id in done:
            return done[number_id][0]
        row = rows.get(number_id)
        if row is None:
            found = con.execute("SELECT input_digest FROM prov.records WHERE number_id = ?", [number_id]).fetchone()
            if found is None:
                raise FamilyError(f"{trail[-1]!r} is built from {number_id!r}, which has no record")
            return found[0]
        if number_id in trail:
            raise FamilyError(f"{number_id!r} is built from itself through {' -> '.join(trail)}")
        if row["kind"] == LEAF:
            done[number_id] = source_digest(con, row["source_query"])
        else:
            children = sorted(set(row["children"]))
            done[number_id] = (pair_digest([(c, of(c, trail + (number_id,))) for c in children]), len(children))
        return done[number_id][0]

    for number_id in rows:
        of(number_id)
    return done


def write_month(con: duckdb.DuckDBPyConnection, month: str, checks_passed: Sequence[str] = (),
                run_id: str | None = None, families: Path | None = None) -> int:
    """Write the provenance of every number published for `month`; returns the count.

    Every family file of `families` (default: the package's own) runs in name order, then the records
    are written in one transaction, replacing the family's earlier records of the month."""
    run_id = run_id or uuid.uuid4().hex
    init(con)
    paths = month_families(families)
    rows: dict[str, dict] = {}
    for path in paths:
        for row in family_rows(con, path, month):
            if row["number_id"] in rows:
                raise FamilyError(f"family {path.stem!r}: {row['number_id']!r} is already produced by another row")
            rows[row["number_id"]] = row
    digests = _digests(con, rows)
    sha, kind = git_sha(), producer_kind()
    records = [
        {"number_id": r["number_id"], "month": r["month"], "value_cents": r["value_cents"],
         "value_text": r["value_text"], "unit": r["unit"], "step": r["step"], "git_sha": sha, "producer_kind": kind,
         "input_digest": digests[n][0], "input_row_count": digests[n][1], "source_query": r["source_query"],
         "checks_passed": list(checks_passed), "signer": None, "signoff_ref": None, "run_id": run_id}
        for n, r in rows.items()
    ]
    con.execute("BEGIN")
    try:
        con.execute("DELETE FROM prov.records WHERE month = ? AND list_contains(?, split_part(number_id, '/', 1))"
                    " AND NOT list_contains(?, number_id)", [month, [p.stem for p in paths], list(rows)])
        # One JSON parameter: binding fifteen values per record one by one is slow.
        con.execute(f"INSERT OR REPLACE INTO prov.records SELECT {', '.join('r.' + c for c in RECORD_TYPES)}"
                    f" FROM (SELECT unnest(from_json(CAST(? AS JSON), '[{RECORD_STRUCT}]')) AS r)",
                    [json.dumps(records)])
        con.execute("COMMIT")
    except BaseException:
        con.execute("ROLLBACK")
        raise
    return len(records)


def result_hash(con: duckdb.DuckDBPyConnection, month: str) -> str | None:
    """SHA-256 over the month's records of the result families, ordered by id; None when it has none.

    Each record contributes its id, value, unit and input digest, so the hash moves when a number or
    anything it was built from does, and not with the commit, run or producer that wrote it."""
    if con.execute("SELECT count(*) FROM information_schema.tables"
                   " WHERE table_schema = 'prov' AND table_name = 'records'").fetchone()[0] == 0:
        return None
    rows = con.execute(
        "SELECT number_id, coalesce(CAST(value_cents AS VARCHAR), value_text, ''), unit, input_digest FROM prov.records"
        " WHERE split_part(number_id, '/', 2) = ? AND list_contains(?, split_part(number_id, '/', 1))"
        " ORDER BY number_id", [month, list(RESULT_FAMILIES)]).fetchall()
    if not rows:
        return None
    return hashlib.sha256("\n".join("\x1f".join(map(str, row)) for row in rows).encode("utf-8")).hexdigest()
