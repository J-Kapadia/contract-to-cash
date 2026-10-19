"""Explain a number: its provenance record, the source rows or child numbers it came from, and whether its digest re-derives."""

from __future__ import annotations

import difflib
from dataclasses import asdict, dataclass, field

import duckdb

from . import prov
from .errors import ProvenanceError, UsageError

SUGGESTIONS = 3
RECORD_COLUMNS = ("number_id", "month", "value_cents", "value_text", "unit", "step", "git_sha", "producer_kind",
                  "input_digest", "input_row_count", "source_query", "checks_passed", "signer", "signoff_ref", "run_id")
# Raw tables that carry no `_row_hash` column: the expression that hashes a row the way the family
# that selects from the table does.
UNHASHED_TABLES = {"ref_policy": "sha256(concat_ws(chr(31), key, value))"}


@dataclass
class SourceRows:
    """The rows of one raw table that a number's source query selected, by `_row_hash`."""
    table: str
    columns: list[str]
    rows: list[list[str | None]]


@dataclass
class Explanation:
    """One node of a number's provenance tree.

    A leaf lists its source rows; a composite lists the explanations of its children, each walked
    one level less deep. `derived_digest` is the digest re-derived from what the node is built on."""
    record: dict
    depth: int
    sources: list[SourceRows] = field(default_factory=list)
    children: list[Explanation] = field(default_factory=list)
    derived_digest: str = ""
    derived_row_count: int = 0

    @property
    def own_match(self) -> bool:
        return self.derived_digest == self.record["input_digest"] and self.derived_row_count == self.record["input_row_count"]

    @property
    def mismatches(self) -> list[Explanation]:
        """The nodes of this tree whose stored digest differs from the re-derived one, parents first."""
        found = [] if self.own_match else [self]
        for child in self.children:
            found.extend(child.mismatches)
        return found

    @property
    def matches(self) -> bool:
        return not self.mismatches

    def to_dict(self) -> dict:
        return {"record": self.record, "depth": self.depth, "sources": [asdict(s) for s in self.sources],
                "children": [c.to_dict() for c in self.children],
                "derived_digest": self.derived_digest, "derived_row_count": self.derived_row_count,
                "digest_matches": self.matches}


def nearest(con: duckdb.DuckDBPyConnection, number_id: str) -> list[str]:
    """Up to three recorded number ids closest to `number_id`, nearest first."""
    known = [r[0] for r in con.execute("SELECT number_id FROM prov.records ORDER BY number_id").fetchall()]
    return difflib.get_close_matches(number_id, known, n=SUGGESTIONS, cutoff=0.5)


class _Walk:
    """The state of one `chain` call: what is looked up once and reused down the tree."""

    def __init__(self, con: duckdb.DuckDBPyConnection) -> None:
        self.con = con
        self._tables: dict[str, str] | None = None
        self._families: dict[tuple[str, str], dict[str, list[str]]] = {}

    def record(self, number_id: str) -> dict:
        row = self.con.execute(f"SELECT {', '.join(RECORD_COLUMNS)} FROM prov.records WHERE number_id = ?",
                               [number_id]).fetchone()
        if row is None:
            close = nearest(self.con, number_id)
            hint = f" Did you mean: {', '.join(close)}?" if close else ""
            raise UsageError(f"no number {number_id!r} in the warehouse.{hint}")
        record = dict(zip(RECORD_COLUMNS, row))
        record["checks_passed"] = list(record["checks_passed"] or [])
        return record

    def tables(self) -> dict[str, str]:
        """Each raw table that carries source rows, with the expression that gives a row's hash."""
        if self._tables is None:
            found = {r[0]: "_row_hash" for r in self.con.execute(
                "SELECT table_name FROM information_schema.columns WHERE table_schema = 'raw'"
                " AND column_name = '_row_hash' ORDER BY table_name").fetchall()}
            found.update(UNHASHED_TABLES)
            self._tables = dict(sorted(found.items()))
        return self._tables

    def source_rows(self, source_query: str) -> list[SourceRows]:
        """The raw rows whose hashes `source_query` selects.

        Only the tables the query names are searched first; the others are searched too when
        that leaves a hash unaccounted for (a query may reach a table through a staging model)."""
        # The hashes stay in the database: binding a few hundred of them as a parameter, once per table, is slow.
        self.con.execute(f"CREATE OR REPLACE TEMP TABLE wanted AS SELECT DISTINCT _row_hash FROM ({source_query})")
        (wanted,) = self.con.execute("SELECT count(*) FROM wanted").fetchone()
        tables = self.tables()
        named = [t for t in tables if t in source_query]
        found: list[SourceRows] = []
        accounted = 0
        for table in named + [t for t in tables if t not in named]:
            if table not in named and accounted >= wanted:
                break
            expression = tables[table]
            select = "*" if table not in UNHASHED_TABLES else f"*, {expression} AS _row_hash"
            cursor = self.con.execute(
                f"SELECT {select} FROM raw.{table} WHERE {expression} IN (SELECT _row_hash FROM wanted) ORDER BY ALL")
            columns = [d[0] for d in cursor.description]
            rows = [list(r) for r in cursor.fetchall()]
            if rows:
                found.append(SourceRows(table, columns, rows))
                accounted += len({r[columns.index("_row_hash")] for r in rows})
        return sorted(found, key=lambda s: s.table)

    def child_ids(self, record: dict) -> list[str]:
        """The ids a composite is computed from, as its family file declares them for its month."""
        number_id = record["number_id"]
        family = number_id.split("/", 1)[0]
        key = (family, record["month"] or "")
        if key not in self._families:
            path = prov.FAMILIES_DIR / f"{family}.sql"
            rows = prov.family_rows(self.con, path, record["month"]) if path.is_file() else []
            self._families[key] = {r["number_id"]: sorted(set(r["children"] or [])) for r in rows
                                   if r["kind"] == prov.COMPOSITE}
        if number_id not in self._families[key]:
            raise ProvenanceError(f"{number_id}: its family no longer declares the children it was built from; "
                                  "close the month again.")
        return self._families[key][number_id]

    def stored_digests(self, ids: list[str]) -> dict[str, str]:
        return dict(self.con.execute("SELECT number_id, input_digest FROM prov.records WHERE list_contains(?, number_id)",
                                     [ids]).fetchall())

    def node(self, number_id: str, depth: int) -> Explanation:
        record = self.record(number_id)
        if record["source_query"]:
            hashes = [r[0] for r in self.con.execute(record["source_query"]).fetchall()]
            node = Explanation(record, depth, derived_digest=prov.digest(hashes), derived_row_count=len(hashes))
            if depth >= 1:
                node.sources = self.source_rows(record["source_query"])
            return node
        ids = self.child_ids(record)
        stored = self.stored_digests(ids)
        missing = [c for c in ids if c not in stored]
        if missing:
            raise ProvenanceError(f"{number_id}: built from {missing[0]!r}, which has no record; close the month again.")
        node = Explanation(record, depth, derived_digest=prov.pair_digest([(c, stored[c]) for c in ids]),
                           derived_row_count=len(ids))
        if depth >= 1:
            node.children = [self.node(c, depth - 1) for c in ids]
        return node


def chain(con: duckdb.DuckDBPyConnection, number_id: str, depth: int = 1) -> Explanation:
    """The provenance tree of `number_id`, walked `depth` levels down.

    A leaf's `source_query` is run again and the digest over its `_row_hash`es re-derived; at `depth`
    1 or more the rows it selects are listed with their table and columns. A composite's digest is
    re-derived from the stored digests of the children its family declares, and at `depth` 1 or more
    each child is explained in turn with `depth - 1`, so `depth` 0 prints a node's record only.
    Raises `UsageError` naming the nearest ids when the number has no record."""
    return _Walk(con).node(number_id, depth)
