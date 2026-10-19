"""Customer-master support: connected components of the merged pairs.

The warehouse computes the clusters in SQL (`int_match_clusters`); `union_find` is the same
computation in Python, so a test can check one against the other and the matcher can cluster pairs
it holds in memory. A node is a `(source_system, source_id)` variant. Both functions are
deterministic: a component is a sorted tuple of its members and the components are sorted by their
first member, whatever order the pairs arrive in.
"""

from __future__ import annotations

import csv
from collections.abc import Hashable, Iterable, Mapping
from dataclasses import asdict, dataclass
from itertools import combinations
from pathlib import Path
from typing import TypeVar

import duckdb

Node = TypeVar("Node", bound=Hashable)

# The decisions that merge a pair: a score at or above the automatic threshold, or a reviewed `same`.
MERGING = ("auto", "same")


def union_find(pairs: Iterable[tuple[Node, Node]], nodes: Iterable[Node] = ()) -> list[tuple[Node, ...]]:
    """The connected components of `pairs`, with every node of `nodes` that no pair names as a
    component of its own. Nodes must be mutually orderable."""
    parent: dict[Node, Node] = {}

    def find(node: Node) -> Node:
        parent.setdefault(node, node)
        root = node
        while parent[root] != root:
            root = parent[root]
        while parent[node] != root:  # compress the path
            parent[node], node = root, parent[node]
        return root

    for node in nodes:
        find(node)
    for a, b in pairs:
        root_a, root_b = find(a), find(b)
        if root_a != root_b:
            # the smaller root wins, so the representative never depends on the pair order
            low, high = sorted((root_a, root_b))
            parent[high] = low

    members: dict[Node, list[Node]] = {}
    for node in parent:
        members.setdefault(find(node), []).append(node)
    return sorted(tuple(sorted(group)) for group in members.values())


def clusters_from_warehouse(con: duckdb.DuckDBPyConnection) -> list[tuple[tuple[str, str], ...]]:
    """The clusters of the warehouse's variants, computed here from the scored pairs rather than
    read from `int_match_clusters`: every variant, joined by the pairs that merge."""
    variants = [tuple(row) for row in con.execute(
        "SELECT source_system, source_id FROM int.int_match_variants").fetchall()]
    pairs = [((sa, a), (sb, b)) for sa, a, sb, b in con.execute(
        "SELECT system_a, source_a, system_b, source_b FROM mart.fct_match_candidates"
        " WHERE decision IN ('auto', 'same')").fetchall()]
    return union_find(pairs, variants)


# -- precision and recall against the answer key ------------------------------------------------

ONE = 10000  # a perfect score, in basis points
IDENTITIES_FILE = "identities.csv"
# The answer key also lists the other spellings the generator wrote (`crm_name`, `billing_name`);
# only these systems are variants of the warehouse.
VARIANT_SYSTEMS = ("crm", "billing", "bank_name", "gl_name")

METRICS = ("precision_auto", "recall_auto", "pairs_doubt", "pairs_reviewed")


def read_identities(truth: str | Path) -> dict[tuple[str, str], str]:
    """The true customer behind every `(source_system, source_id)` variant, from the answer key."""
    with (Path(truth) / IDENTITIES_FILE).open(encoding="utf-8", newline="") as fh:
        return {(r["source_system"], r["source_id"]): r["true_customer_id"]
                for r in csv.DictReader(fh) if r["source_system"] in VARIANT_SYSTEMS}


def _pairs_within(groups: Iterable[Iterable[Node]]) -> set[frozenset[Node]]:
    return {frozenset(pair) for group in groups for pair in combinations(sorted(group), 2)}


def pairwise_bps(clusters: Iterable[Iterable[Node]], truth: Mapping[Node, str]) -> tuple[int, int]:
    """`(precision, recall)` in basis points of the pairs inside `clusters` against the pairs inside
    the same true customer. Both round down, so only an exact match scores 10000. With no pair
    on one side the ratio is empty, which scores 10000. A pair of two variants that the answer
    key does not know (a vendor and its own spellings) is no customer pair and is left out."""
    by_owner: dict[str, list[Node]] = {}
    for node, owner in truth.items():
        by_owner.setdefault(owner, []).append(node)
    found = {pair for pair in _pairs_within(clusters) if any(node in truth for node in pair)}
    wanted = _pairs_within(by_owner.values())
    hits = len(found & wanted)
    return (hits * ONE // len(found) if found else ONE, hits * ONE // len(wanted) if wanted else ONE)


def clusters_deciding(con: duckdb.DuckDBPyConnection, decisions: Iterable[str]) -> list[tuple[tuple[str, str], ...]]:
    """The clusters of the variants when only pairs decided as one of `decisions` merge."""
    variants = [tuple(row) for row in con.execute(
        "SELECT source_system, source_id FROM int.int_match_variants").fetchall()]
    listed = list(decisions)
    pairs = [((sa, a), (sb, b)) for sa, a, sb, b in con.execute(
        "SELECT system_a, source_a, system_b, source_b FROM mart.fct_match_candidates"
        " WHERE decision IN (SELECT unnest(?))", [listed]).fetchall()]
    return union_find(pairs, variants)


@dataclass(frozen=True)
class Scores:
    """Precision and recall in basis points: the automatic matcher alone, and after review."""
    precision_auto: int
    recall_auto: int
    precision_reviewed: int
    recall_reviewed: int
    pairs_doubt: int
    pairs_reviewed: int

    def to_dict(self) -> dict[str, int]:
        return asdict(self)

    def shortfalls(self) -> list[str]:
        """What is below 10000, as `name bps` phrases; empty when the check passes."""
        wanted = {"automatic precision": self.precision_auto, "precision after review": self.precision_reviewed,
                  "recall after review": self.recall_reviewed}
        return [f"{name} {value} bps" for name, value in wanted.items() if value < ONE]


def score(con: duckdb.DuckDBPyConnection, truth: Mapping[tuple[str, str], str]) -> Scores:
    """Pairwise precision and recall of the warehouse's clusters against `truth`."""
    precision_auto, recall_auto = pairwise_bps(clusters_deciding(con, ("auto",)), truth)
    precision_reviewed, recall_reviewed = pairwise_bps(clusters_deciding(con, MERGING), truth)
    doubt, reviewed = con.execute(
        "SELECT count(*), count(*) FILTER (WHERE decision IN ('same', 'different'))"
        " FROM mart.fct_match_candidates WHERE decision IS NULL OR decision IN ('same', 'different')").fetchone()
    return Scores(precision_auto, recall_auto, precision_reviewed, recall_reviewed, doubt, reviewed)


def write_rollup(con: duckdb.DuckDBPyConnection, month: str, scores: Scores) -> None:
    """Replace the month's `master/*` rows of `mart.fct_rollup` with the automatic matcher's scores
    and the doubt-band counts."""
    values = scores.to_dict()
    con.execute("DELETE FROM mart.fct_rollup WHERE month = ? AND metric LIKE 'master/%'", [month])
    for metric in METRICS:
        unit = "pairs" if metric.startswith("pairs") else "bps"
        con.execute("INSERT INTO mart.fct_rollup (month, metric, value_cents, value_text) VALUES (?, ?, ?, ?)",
                    [month, f"master/{metric}", values[metric], f"{values[metric]} {unit}"])


def candidates(con: duckdb.DuckDBPyConnection) -> list[dict]:
    """Every candidate pair with its score, the rules that fired and its decision (null while unreviewed)."""
    rows = con.execute("SELECT system_a, source_a, system_b, source_b, score, rules_fired, decision"
                       " FROM mart.fct_match_candidates ORDER BY system_a, source_a, system_b, source_b").fetchall()
    return [{"a": {"system": sa, "id": a}, "b": {"system": sb, "id": b}, "score": s,
             "rules_fired": list(rules), "decision": decision} for sa, a, sb, b, s, rules, decision in rows]
