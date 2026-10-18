"""Loading the books and the reference files into the warehouse's `raw` schema.

Every column lands as VARCHAR. Each row also gets `_row_hash` (the SHA-256 of its columns joined
by the unit separator, computed once in SQL), `_month` (the month the row belongs to) and
`_load_id`. The books carry their own `known_on`.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import duckdb

from .errors import PreconditionError, UsageError

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


def _load_file(con: duckdb.DuckDBPyConnection, path: Path, table: str, extra: str, where: str = "") -> int:
    source = "read_csv(?, header = true, all_varchar = true)"
    columns = [row[0] for row in con.execute(f"DESCRIBE SELECT * FROM {source}", [str(path)]).fetchall()]
    joined = ", ".join(f"coalesce({_quote(c)}, '')" for c in columns)
    con.execute(
        f"CREATE OR REPLACE TABLE {table} AS "
        f"SELECT *, sha256(concat_ws(chr(31), {joined})) AS _row_hash{extra} FROM {source} {where}",
        [str(path)],
    )
    return con.execute(f"SELECT count(*) FROM {table}").fetchone()[0]


def load_months(con: duckdb.DuckDBPyConnection, books: str | Path, first: str, last: str,
                load_id: str) -> dict[str, int]:
    """Load every books file present, with the rows whose month is on or before `last`, into
    `raw.<stem>`; returns the row count per table.

    The tables are rebuilt each time. Rows dated before `first` (a contract signed before the
    books' window opens) are history the window's rows refer to, so they load with their own
    month. A books file needs `known_on` and its month column."""
    if check_month(first) > check_month(last):
        raise UsageError(f"the first month {first} is after the last month {last}.")
    books = Path(books)
    con.execute("CREATE SCHEMA IF NOT EXISTS raw")
    counts: dict[str, int] = {}
    for stem, month_column in MONTH_COLUMN.items():
        path = books / f"{stem}.csv"
        if not path.is_file():
            continue
        header = {row[0] for row in con.execute(
            "DESCRIBE SELECT * FROM read_csv(?, header = true, all_varchar = true)", [str(path)]).fetchall()}
        for needed in ("known_on", month_column):
            if needed not in header:
                raise UsageError(f"{path} has no {needed} column.")
        month = f"substr({_quote(month_column)}, 1, 7)"
        load_id_sql = "'" + load_id.replace("'", "''") + "'"
        counts[stem] = _load_file(
            con, path, f"raw.{stem}",
            f", {month} AS _month, {load_id_sql} AS _load_id",
            where=f"WHERE {month} <= '{check_month(last)}'",
        )
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
