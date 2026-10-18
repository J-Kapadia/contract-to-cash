"""Explain a number: its provenance record, the source rows it came from, and whether its digest re-derives."""

from __future__ import annotations

import difflib
from dataclasses import asdict, dataclass, field

import duckdb

from . import prov
from .errors import UsageError

SUGGESTIONS = 3
RECORD_COLUMNS = ("number_id", "month", "value_cents", "value_text", "unit", "step", "git_sha", "producer_kind",
                  "input_digest", "input_row_count", "source_query", "checks_passed", "signer", "signoff_ref", "run_id")


@dataclass
class SourceRows:
    """The rows of one raw table that a number's source query selected, by `_row_hash`."""
    table: str
    columns: list[str]
    rows: list[list[str | None]]


@dataclass
class Explanation:
    record: dict
    depth: int
    sources: list[SourceRows] = field(default_factory=list)
    derived_digest: str = ""
    derived_row_count: int = 0

    @property
    def matches(self) -> bool:
        return self.derived_digest == self.record["input_digest"] and self.derived_row_count == self.record["input_row_count"]

    def to_dict(self) -> dict:
        return {"record": self.record, "depth": self.depth, "sources": [asdict(s) for s in self.sources],
                "derived_digest": self.derived_digest, "derived_row_count": self.derived_row_count,
                "digest_matches": self.matches}


def nearest(con: duckdb.DuckDBPyConnection, number_id: str) -> list[str]:
    """Up to three recorded number ids closest to `number_id`, nearest first."""
    known = [r[0] for r in con.execute("SELECT number_id FROM prov.records ORDER BY number_id").fetchall()]
    return difflib.get_close_matches(number_id, known, n=SUGGESTIONS, cutoff=0.5)


def _raw_tables(con: duckdb.DuckDBPyConnection) -> list[str]:
    return [r[0] for r in con.execute(
        "SELECT table_name FROM information_schema.columns WHERE table_schema = 'raw' AND column_name = '_row_hash'"
        " ORDER BY table_name").fetchall()]


def _source_rows(con: duckdb.DuckDBPyConnection, hashes: list[str]) -> list[SourceRows]:
    wanted = sorted(set(hashes))
    found = []
    for table in _raw_tables(con):
        cursor = con.execute(f"SELECT * FROM raw.{table} WHERE _row_hash IN (SELECT unnest(?)) ORDER BY ALL", [wanted])
        columns = [d[0] for d in cursor.description]
        rows = [list(r) for r in cursor.fetchall()]
        if rows:
            found.append(SourceRows(table, columns, rows))
    return found


def chain(con: duckdb.DuckDBPyConnection, number_id: str, depth: int = 1) -> Explanation:
    """The provenance chain of `number_id`.

    The record's `source_query` is run again; at `depth` 1 or more the rows it selects are listed with
    their table and columns, and at every depth the digest over their `_row_hash`es is re-derived.
    Raises `UsageError` naming the nearest ids when the number has no record."""
    row = con.execute(f"SELECT {', '.join(RECORD_COLUMNS)} FROM prov.records WHERE number_id = ?", [number_id]).fetchone()
    if row is None:
        close = nearest(con, number_id)
        hint = f" Did you mean: {', '.join(close)}?" if close else ""
        raise UsageError(f"no number {number_id!r} in the warehouse.{hint}")
    record = dict(zip(RECORD_COLUMNS, row))
    record["checks_passed"] = list(record["checks_passed"] or [])
    hashes = [r[0] for r in con.execute(record["source_query"]).fetchall()]
    explanation = Explanation(record, depth, derived_digest=prov.digest(hashes), derived_row_count=len(hashes))
    if depth >= 1:
        explanation.sources = _source_rows(con, hashes)
    return explanation
