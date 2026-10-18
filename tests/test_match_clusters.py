"""Clusters and the customer master: the connected components of the merged pairs.

The components are computed in SQL by dbt; `c2c.master.union_find` computes the same thing in Python,
and the tests hold the two against each other over the `mini` books and over handmade pairs.
"""

from __future__ import annotations

import random
import shutil
from pathlib import Path

import pytest
from hypothesis import given, settings, strategies as st

from c2c import db, dbt_runner, master
from conftest import shared_dir
from test_match_normalisation import designed_pairs, mini_loaded, query  # noqa: F401
from test_match_scoring import identities, mini_scored  # noqa: F401

MERGED = "SELECT system_a, source_a, system_b, source_b FROM mart.fct_match_candidates WHERE decision IN ('auto', 'same')"
SEP = "\x1f"


def sql_components(warehouse: Path) -> list[tuple[tuple[str, str], ...]]:
    groups: dict[str, list[tuple[str, str]]] = {}
    for system, source_id, cluster in query(
            warehouse, "SELECT source_system, source_id, cluster_id FROM int.int_match_clusters"):
        groups.setdefault(cluster, []).append((system, source_id))
    return sorted(tuple(sorted(g)) for g in groups.values())


# ------------------------------------------------------------------ union_find


def test_union_find_joins_chains_and_keeps_loners():
    got = master.union_find([("b", "c"), ("a", "b"), ("x", "y"), ("c", "a")], nodes=["z", "a"])
    assert got == [("a", "b", "c"), ("x", "y"), ("z",)]


def test_union_find_of_nothing_is_nothing():
    assert master.union_find([]) == []


@settings(max_examples=200, deadline=None)
@given(st.lists(st.tuples(st.integers(0, 12), st.integers(0, 12)), max_size=30), st.randoms(use_true_random=False))
def test_union_find_is_the_same_under_any_order_of_the_pairs(pairs, rnd):
    expected = master.union_find(pairs)
    shuffled = list(pairs)
    rnd.shuffle(shuffled)
    flipped = [(b, a) for a, b in reversed(shuffled)]
    assert master.union_find(shuffled) == expected == master.union_find(flipped)
    assert sorted(n for group in expected for n in group) == sorted({n for pair in pairs for n in pair})


# ------------------------------------------------------------------ the mini books


@pytest.fixture(scope="session")
def mini_clusters(tmp_path_factory, mini_scored) -> Path:  # noqa: F811
    """The scored `mini` warehouse with the clusters and the customer master built."""

    def build(directory: Path) -> None:
        shutil.copy(mini_scored, directory / "w.duckdb")
        dbt_runner.build(directory / "w.duckdb", select="int_match_clusters dim_customer int_customer_members")

    return shared_dir(tmp_path_factory, "clusters-mini", build) / "w.duckdb"


def test_the_sql_components_equal_union_find_over_the_same_pairs(mini_clusters):
    pairs = [((sa, a), (sb, b)) for sa, a, sb, b in query(mini_clusters, MERGED)]
    variants = [tuple(v) for v in query(mini_clusters, "SELECT source_system, source_id FROM int.int_match_variants")]
    assert master.union_find(pairs, variants) == sql_components(mini_clusters)


def test_clusters_from_warehouse_agree_with_the_sql(mini_clusters):
    con = db.connect(mini_clusters, read_only=True)
    try:
        assert master.clusters_from_warehouse(con) == sql_components(mini_clusters)
    finally:
        con.close()


def test_every_source_record_is_in_exactly_one_cluster(mini_clusters):
    variants = query(mini_clusters, "SELECT source_system, source_id FROM int.int_match_variants ORDER BY ALL")
    members = query(mini_clusters, "SELECT source_system, source_id FROM int.int_match_clusters ORDER BY ALL")
    assert members == variants
    assert len(set(members)) == len(members)
    assert len(query(mini_clusters, "SELECT 1 FROM int.int_customer_members")) == len(variants)


def test_each_customer_of_the_answer_key_is_one_cluster(mini_clusters):
    owner = identities()
    clusters = sql_components(mini_clusters)
    by_variant = {v: i for i, group in enumerate(clusters) for v in group}
    for customer in sorted(set(owner.values())):
        variants = [v for v, c in owner.items() if c == customer]
        assert len(variants) >= 4
        assert len({by_variant[v] for v in variants}) == 1, customer
    # no cluster holds two customers; the rest are payees that are no customer
    holding = [group for group in clusters if any(v in owner for v in group)]
    assert len(holding) == len(set(owner.values()))
    for group in holding:
        assert len({owner[v] for v in group if v in owner}) == 1


def test_the_four_variant_customer_is_one_cluster_with_its_crm_id(mini_clusters):
    got = {c: members for c, members in query(
        mini_clusters, "SELECT customer_id, member_count FROM mart.dim_customer")}
    owner = identities()
    sizes = {}
    for customer in owner.values():
        sizes[customer] = sizes.get(customer, 0) + 1
    assert got == sizes


def test_the_master_has_a_row_per_cluster_named_by_its_crm_account(mini_clusters):
    rows = query(mini_clusters, "SELECT customer_id, display_name, review_state FROM mart.dim_customer"
                                " ORDER BY customer_id")
    assert len(rows) == len(set(identities().values()))
    names = dict(query(mini_clusters, "SELECT crm_account_id, account_name FROM int.stg_crm_accounts"))
    assert all(display_name == names[customer_id] for customer_id, display_name, _ in rows)
    # a customer is reviewed when a reviewed `same` joins its variants, auto otherwise
    reviewed = {x for a, b, d in designed_pairs() if d == "same" for x in (a, b) if x.startswith("acct_")}
    assert reviewed
    assert {c: s for c, _, s in rows} == {c: "reviewed" if c in reviewed else "auto" for c, _, _ in rows}


def test_members_resolve_to_their_customer(mini_clusters):
    owner = identities()
    got = {(s, i): c for s, i, c in query(
        mini_clusters, "SELECT source_system, source_id, customer_id FROM int.int_customer_members")}
    for variant, customer in owner.items():
        assert got[variant] == customer


def test_a_doubt_pair_without_a_decision_leaves_its_clusters_pending(tmp_path, mini_clusters):
    warehouse = tmp_path / "w.duckdb"
    shutil.copy(mini_clusters, warehouse)
    con = db.connect(warehouse)
    try:
        con.execute("DELETE FROM raw.ref_customer_matches")
    finally:
        con.close()
    dbt_runner.build(warehouse, select="fct_match_candidates int_match_clusters dim_customer int_customer_members")
    rows = query(warehouse, "SELECT customer_id, review_state, member_count FROM mart.dim_customer")
    pending = {c for c, s, _ in rows if s == "pending"}
    undecided = {x for a, b, _ in designed_pairs() for x in (a, b) if x.startswith("acct_")}
    assert undecided <= pending
    assert pending == undecided | {"acct_0003"}  # the other designed pair names a billing record of acct_0003
    assert len(rows) == len(set(identities().values()))


# ------------------------------------------------------------------ handmade pairs through dbt


def scratch_clusters(directory: Path, variants: list[tuple[str, str]], pairs: list[tuple]) -> list:
    """Run `int_match_clusters` over handmade variants and merged pairs, given in the order listed."""
    warehouse = directory / "w.duckdb"
    con = db.connect(warehouse)
    try:
        db.init_schemas(con)
        con.execute("CREATE SCHEMA IF NOT EXISTS int")
        con.execute("CREATE TABLE int.int_match_variants (source_system VARCHAR, source_id VARCHAR)")
        con.executemany("INSERT INTO int.int_match_variants VALUES (?, ?)", variants)
        con.execute("CREATE TABLE mart.fct_match_candidates (system_a VARCHAR, source_a VARCHAR,"
                    " system_b VARCHAR, source_b VARCHAR, decision VARCHAR)")
        con.executemany("INSERT INTO mart.fct_match_candidates VALUES (?, ?, ?, ?, ?)", pairs)
    finally:
        con.close()
    dbt_runner.build(warehouse, select="int_match_clusters")
    return sql_components(warehouse)


def test_sql_clusters_do_not_depend_on_the_order_of_the_pairs(tmp_path):
    variants = [("crm", "a1"), ("billing", "b1"), ("bank_name", "b1"), ("gl_name", "g1"), ("gl_name", "g2"),
                ("billing", "b2"), ("crm", "a2")]
    pairs = [("crm", "a1", "billing", "b1", "auto"), ("billing", "b1", "gl_name", "g1", "same"),
             ("bank_name", "b1", "gl_name", "g1", "auto"), ("gl_name", "g2", "billing", "b2", "auto"),
             ("billing", "b2", "crm", "a2", "different"), ("crm", "a2", "gl_name", "g2", "none"),
             ("crm", "a1", "gl_name", "g1", None)]
    merged = [((sa, a), (sb, b)) for sa, a, sb, b, d in pairs if d in ("auto", "same")]
    expected = master.union_find(merged, variants)
    assert len(expected) == 3
    orders = [pairs, pairs[::-1], random.Random(7).sample(pairs, len(pairs))]
    for n, order in enumerate(orders):
        directory = tmp_path / str(n)
        directory.mkdir()
        assert scratch_clusters(directory, variants if n == 0 else variants[::-1], order) == expected
