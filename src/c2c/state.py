"""Month state: which months are open, closed or locked, and what each was closed on.

`state.close_state` holds one row per month (`status`, the `input_digest` of the books rows it
was built from, its `result_hash`, and `restated_from`, the hash a recompute replaced).
`state.books` holds the one books identity the warehouse belongs to. A month with no row is open.
`result_hash` stays null until a result hash is defined and passed to `transition`.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterator
from pathlib import Path

import duckdb

from . import prov
from .errors import InternalError, UsageError

OPEN, CLOSED, LOCKED = "open", "closed", "locked"
STATUSES = (OPEN, CLOSED, LOCKED)

# (from, to): a month is closed from open, closed again when its inputs changed, locked once closed,
# and reopened when a recompute of it fails its controls.
TRANSITIONS = {(OPEN, OPEN), (OPEN, CLOSED), (CLOSED, CLOSED), (CLOSED, LOCKED), (CLOSED, OPEN)}

COLUMNS = ("status", "input_digest", "result_hash", "restated_from")


def init(con: duckdb.DuckDBPyConnection) -> None:
    """Create the state tables when they are not there yet."""
    con.execute("CREATE SCHEMA IF NOT EXISTS state")
    con.execute("CREATE TABLE IF NOT EXISTS state.close_state (month VARCHAR PRIMARY KEY, status VARCHAR NOT NULL,"
                " input_digest VARCHAR, result_hash VARCHAR, restated_from VARCHAR)")
    con.execute("CREATE TABLE IF NOT EXISTS state.books (books_id VARCHAR)")


def _exists(con: duckdb.DuckDBPyConnection, schema: str, table: str) -> bool:
    return con.execute("SELECT count(*) FROM information_schema.tables WHERE table_schema = ? AND table_name = ?",
                       [schema, table]).fetchone()[0] > 0


def months(first: str, last: str) -> Iterator[str]:
    """Every month from `first` through `last`, as `YYYY-MM`, in order."""
    year, month = int(first[:4]), int(first[5:7])
    while f"{year:04d}-{month:02d}" <= last:
        yield f"{year:04d}-{month:02d}"
        year, month = (year + 1, 1) if month == 12 else (year, month + 1)


def books_id(books: str | Path) -> str | None:
    """The identity of a books directory: the SHA-256 of its manifest, which sits beside it as
    `<name>.manifest.sha256`; None for books without one (a hand-made or edited copy)."""
    books = Path(books)
    manifest = books.parent / f"{books.name}.manifest.sha256"
    return hashlib.sha256(manifest.read_bytes()).hexdigest() if manifest.is_file() else None


def check_books(con: duckdb.DuckDBPyConnection, books: str | Path) -> None:
    """Record which books the warehouse holds; exit 2 when it already holds different ones.

    Books without a manifest have no identity, so they are neither recorded nor refused."""
    init(con)
    found = books_id(books)
    if found is None:
        return
    recorded = [row[0] for row in con.execute("SELECT books_id FROM state.books").fetchall()]
    if not recorded:
        con.execute("INSERT INTO state.books VALUES (?)", [found])
    elif recorded != [found]:
        raise UsageError(f"the warehouse holds books {recorded[0][:12]}, not {found[:12]} ({books}): "
                         "use a new warehouse for these books, or delete this one.")


def get(con: duckdb.DuckDBPyConnection, month: str) -> dict | None:
    """The month's row, or None when it has none."""
    if not _exists(con, "state", "close_state"):
        return None
    row = con.execute(f"SELECT {', '.join(COLUMNS)} FROM state.close_state WHERE month = ?", [month]).fetchone()
    return None if row is None else dict(zip(COLUMNS, row))


def status(con: duckdb.DuckDBPyConnection, month: str) -> str:
    """The month's status; `open` when it has no row."""
    row = get(con, month)
    return OPEN if row is None else row["status"]


def transition(con: duckdb.DuckDBPyConnection, month: str, to: str, input_digest: str | None = None,
               result_hash: str | None = None, restated_from: str | None = None) -> None:
    """Move `month` to `to`, storing the digest, hash and restated-from given (a move to `open`
    clears them)."""
    if to not in STATUSES:
        raise InternalError(f"{to!r} is not a month status.")
    init(con)
    current = status(con, month)
    if (current, to) not in TRANSITIONS:
        raise InternalError(f"{month} is {current}; it cannot become {to}.")
    if to == OPEN:
        input_digest = result_hash = restated_from = None
    con.execute("INSERT OR REPLACE INTO state.close_state VALUES (?, ?, ?, ?, ?)",
                [month, to, input_digest, result_hash, restated_from])


def input_digest(con: duckdb.DuckDBPyConnection, month: str) -> str:
    """SHA-256 over the row hashes of every `raw.*` row that belongs to `month`."""
    tables = [row[0] for row in con.execute(
        "SELECT table_name FROM information_schema.columns WHERE table_schema = 'raw' AND column_name = '_month'"
        " ORDER BY table_name").fetchall()]
    hashes = [f"{table}:{row[0]}" for table in tables
              for row in con.execute(f'SELECT _row_hash FROM raw."{table}" WHERE _month = ?', [month]).fetchall()]
    return prov.digest(hashes)
