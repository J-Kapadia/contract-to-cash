"""Customer-master support: connected components of the merged pairs.

The warehouse computes the clusters in SQL (`int_match_clusters`); `union_find` is the same
computation in Python, so a test can check one against the other and the matcher can cluster pairs
it holds in memory. A node is a `(source_system, source_id)` variant. Both functions are
deterministic: a component is a sorted tuple of its members and the components are sorted by their
first member, whatever order the pairs arrive in.
"""

from __future__ import annotations

from collections.abc import Hashable, Iterable
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
