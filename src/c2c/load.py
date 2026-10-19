"""Loading the books and the reference files into the warehouse's `raw` schema.

Every column lands as VARCHAR. Each row also gets `_row_hash` (the SHA-256 of its columns joined
by the unit separator, computed once in SQL), `_month` (the month the row belongs to) and
`_load_id`. The books carry their own `known_on`.
"""

from __future__ import annotations

import dataclasses
import json
import re
from pathlib import Path

import duckdb

from . import controls, prov, state
from . import policy as policy_config
from .errors import ControlFailure, PreconditionError, UsageError

# File stem -> the column whose first seven characters (`YYYY-MM`) are the row's month. Rows
# without a dated event of their own (accounts, contacts, lines, billing customers, commitments)
# belong to the month they entered the source system.
MONTH_COLUMN = {
    "crm_accounts": "known_on",
    "crm_contacts": "known_on",
    "pipeline_snapshots": "snapshot_date",
    "contracts": "signed_on",
    "contract_lines": "known_on",
    "usage_monthly": "month",
    "billing_customers": "known_on",
    "invoices": "invoice_date",
    "payments": "applied_on",
    "bank_transactions": "posted_on",
    "gl": "gl_period",
    "payroll_runs": "pay_date",
    "headcount_plan": "known_on",
    "commission_plan": "known_on",
    "ap_bills": "bill_date",
    "vendor_commitments": "known_on",
}

MONTH_PATTERN = re.compile(r"\d{4}-(0[1-9]|1[0-2])")
HASH_SEPARATOR = "\x1f"


def _quote(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def check_month(month: str) -> str:
    if not MONTH_PATTERN.fullmatch(month):
        raise UsageError(f"{month!r} is not a month; write it as YYYY-MM.")
    return month


def window(books: str | Path) -> tuple[str, str]:
    """The first and last month the books cover, from `window.json`."""
    path = Path(books) / "window.json"
    if not path.is_file():
        raise PreconditionError(f"no books at {Path(books)}: {path.name} is missing; generate them first.")
    data = json.loads(path.read_text(encoding="utf-8"))
    return check_month(data["first_month"]), check_month(data["last_month"])


def check_in_window(books: str | Path, month: str) -> tuple[str, str]:
    """The books' window, after checking that `month` lies in it."""
    first, last = window(books)
    if not first <= check_month(month) <= last:
        raise UsageError(f"{month} is outside the books' window {first}..{last}; choose a month in it.")
    return first, last


SOURCE = "read_csv(?, header = true, all_varchar = true)"


def _row_hash_sql(con: duckdb.DuckDBPyConnection, path: Path) -> str:
    """The SQL expression for the `_row_hash` of a row of the CSV at `path`."""
    columns = [row[0] for row in con.execute(f"DESCRIBE SELECT * FROM {SOURCE}", [str(path)]).fetchall()]
    joined = ", ".join(f"coalesce({_quote(c)}, '')" for c in columns)
    return f"sha256(concat_ws(chr(31), {joined}))"


def _load_file(con: duckdb.DuckDBPyConnection, path: Path, table: str, extra: str, where: str = "") -> int:
    con.execute(
        f"CREATE OR REPLACE TABLE {table} AS "
        f"SELECT *, {_row_hash_sql(con, path)} AS _row_hash{extra} FROM {SOURCE} {where}",
        [str(path)],
    )
    return con.execute(f"SELECT count(*) FROM {table}").fetchone()[0]


def _check_header(con: duckdb.DuckDBPyConnection, path: Path, month_column: str) -> None:
    header = {row[0] for row in con.execute(f"DESCRIBE SELECT * FROM {SOURCE}", [str(path)]).fetchall()}
    for needed in ("known_on", month_column):
        if needed not in header:
            raise UsageError(f"{path} has no {needed} column.")


def check_locked(con: duckdb.DuckDBPyConnection, books: str | Path, last: str) -> None:
    """Fail with CTL-LOCK when the books' rows of a locked month, up to `last`, no longer hash to the
    digest recorded when it was locked. Reads the files and writes nothing."""
    books = Path(books)
    locked = {m: d for m, d in state.locked_months(con).items() if m <= last}
    if not locked:
        return
    lines: dict[str, list[str]] = {m: [] for m in locked}
    for stem, month_column in MONTH_COLUMN.items():
        path = books / f"{stem}.csv"
        if not path.is_file():
            continue
        _check_header(con, path, month_column)
        month = f"substr({_quote(month_column)}, 1, 7)"
        rows = con.execute(f"SELECT {month}, {_row_hash_sql(con, path)} FROM {SOURCE} WHERE {month} IN "
                           f"({', '.join('?' for _ in locked)})", [str(path), *locked]).fetchall()
        for m, row_hash in rows:
            lines[m].append(f"{stem}:{row_hash}")
    changed = [m for m in locked if prov.digest(lines[m]) != locked[m]]
    if not changed:
        return
    template, _ = controls._header(controls.CONTROLS_DIR / "CTL-LOCK.sql")
    remediation = [controls._fill(template, ["month"], (m,)) for m in changed]
    named = ", ".join(changed)
    raise ControlFailure(f"locked month{'s' if len(changed) > 1 else ''} {named} changed: CTL-LOCK failed.", [
        {"id": "CTL-LOCK", "violating_rows": len(changed), "detail": {"row_hashes": [], "number_ids": []},
         "remediation": remediation}])


def load_months(con: duckdb.DuckDBPyConnection, books: str | Path, first: str, last: str,
                load_id: str) -> dict[str, int]:
    """Load every books file present, with the rows whose month is on or before `last`, into
    `raw.<stem>`; returns the row count per table.

    The tables are rebuilt each time. Rows dated before `first` (a contract signed before the
    books' window opens) are history the window's rows refer to, so they load with their own
    month. A books file needs `known_on` and its month column. A locked month whose rows changed
    fails with CTL-LOCK before anything is written."""
    if check_month(first) > check_month(last):
        raise UsageError(f"the first month {first} is after the last month {last}.")
    books = Path(books)
    check_locked(con, books, last)
    con.execute("CREATE SCHEMA IF NOT EXISTS raw")
    counts: dict[str, int] = {}
    for stem, month_column in MONTH_COLUMN.items():
        path = books / f"{stem}.csv"
        if not path.is_file():
            continue
        _check_header(con, path, month_column)
        month = f"substr({_quote(month_column)}, 1, 7)"
        load_id_sql = "'" + load_id.replace("'", "''") + "'"
        counts[stem] = _load_file(
            con, path, f"raw.{stem}",
            f", {month} AS _month, {load_id_sql} AS _load_id",
            where=f"WHERE {month} <= '{check_month(last)}'",
        )
    state.define_row_view(con)
    return counts


def load_reference(con: duckdb.DuckDBPyConnection, directory: str | Path) -> dict[str, int]:
    """Load every `*.csv` of the reference directory into `raw.ref_<stem>`; a directory that does
    not exist loads nothing."""
    directory = Path(directory)
    con.execute("CREATE SCHEMA IF NOT EXISTS raw")
    counts: dict[str, int] = {}
    for path in sorted(directory.glob("*.csv")):
        counts[f"ref_{path.stem}"] = _load_file(con, path, f"raw.ref_{path.stem}", "")
    return counts


def _flatten(prefix: str, value: object) -> list[tuple[str, int]]:
    """The integers under `value` as `(dotted key, value)`; a list item's key ends in its position."""
    if dataclasses.is_dataclass(value):
        value = dataclasses.asdict(value)
    if isinstance(value, dict):
        return [item for k, v in value.items() for item in _flatten(f"{prefix}.{k}" if prefix else k, v)]
    if isinstance(value, (list, tuple)):
        return [item for i, v in enumerate(value) for item in _flatten(f"{prefix}.{i}", v)]
    return [(prefix, value)]


def load_config(con: duckdb.DuckDBPyConnection, config_dir: str | Path) -> dict[str, int]:
    """Load the checked configuration into the warehouse so SQL reads one source: `raw.ref_policy`
    (`key`, `value`: every policy value under its dotted key, such as `match_weights.email_exact`)
    and `raw.ref_customer_matches` (`a`, `b`, `decision`, `reason`, `_row_hash`). Both tables are
    rebuilt each time; returns the row count of each."""
    policy = policy_config.load(config_dir)
    matches = policy_config.load_matches(config_dir)
    con.execute("CREATE SCHEMA IF NOT EXISTS raw")
    con.execute("CREATE OR REPLACE TABLE raw.ref_policy (key VARCHAR, value BIGINT)")
    con.executemany("INSERT INTO raw.ref_policy VALUES (?, ?)", _flatten("", policy))
    con.execute("CREATE OR REPLACE TEMPORARY TABLE _matches (a VARCHAR, b VARCHAR, decision VARCHAR, reason VARCHAR)")
    for m in matches:
        con.execute("INSERT INTO _matches VALUES (?, ?, ?, ?)", [m.a, m.b, m.decision, m.reason])
    con.execute(
        "CREATE OR REPLACE TABLE raw.ref_customer_matches AS "
        "SELECT *, sha256(concat_ws(chr(31), a, b, decision, reason)) AS _row_hash FROM _matches")
    con.execute("DROP TABLE _matches")
    return {table: con.execute(f"SELECT count(*) FROM raw.{table}").fetchone()[0]
            for table in ("ref_policy", "ref_customer_matches")}
