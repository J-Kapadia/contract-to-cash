"""Scoring candidate pairs: the rules, the score, the doubt band and the decision.

The truth table runs the model over handmade variants in a scratch warehouse; the `mini` tests run
it over the committed books with the reviewed decisions of its designed doubt pairs.
"""

from __future__ import annotations

import csv
import shutil
from pathlib import Path

import pytest
import yaml

from c2c import db, dbt_runner, load
from conftest import MINI_TRUTH, ROOT, shared_dir
from test_match_normalisation import designed_pairs, mini_loaded, query  # noqa: F401

CONFIG = ROOT / "config"
SCORE_SQL = ("SELECT system_a, source_a, system_b, source_b, score, rules_fired, decision"
             " FROM mart.fct_match_candidates")


def scratch_config(directory: Path, pairs=(), weights: dict[str, int] | None = None) -> Path:
    """A configuration directory holding the committed policy (with `weights` over its match
    weights) and the reviewed `pairs`."""
    directory.mkdir(parents=True, exist_ok=True)
    policy = yaml.safe_load((CONFIG / "policy.yaml").read_text())
    policy["match_weights"].update(weights or {})
    (directory / "policy.yaml").write_text(yaml.safe_dump(policy, sort_keys=False))
    (directory / "customer_matches.yaml").write_text(yaml.safe_dump({"pairs": list(pairs)}, sort_keys=False))
    return directory


def score(warehouse: Path, config: Path, select: str = "fct_match_candidates") -> None:
    """Load `config` into the warehouse and build `select`: by default the scoring model alone, over
    what is already there."""
    con = db.connect(warehouse)
    try:
        load.load_config(con, config)
    finally:
        con.close()
    dbt_runner.build(warehouse, select=select)


def rows_by_pair(warehouse: Path) -> dict[frozenset, tuple]:
    """Score, rules and decision per pair, the pair being its two (system, id) variants."""
    return {frozenset(((sa, a), (sb, b))): (s, rules, d) for sa, a, sb, b, s, rules, d in query(warehouse, SCORE_SQL)}


def variant_of(source_id: str) -> tuple[str, str]:
    """The (system, id) of a CRM account or billing customer id, as the reviewed file names them."""
    return ("crm" if source_id.startswith("acct_") else "billing", source_id)


# ------------------------------------------------------------------ the truth table

# Each case is two variants and what the scorer must say about them. A variant is
# (system, id, normalised name, domain, contact emails); tokens are the name's words.
NO_DOMAIN = None
CASES = [
    # name, variant a, variant b, rules fired, score, decision
    ("shared email only",
     ("crm", "c01a", "alpha tools", NO_DOMAIN, ["k@mail.example"]),
     ("billing", "c01b", "zeta works", NO_DOMAIN, ["k@mail.example"]),
     ["email_exact"], 60, "auto"),
    ("different emails",
     ("crm", "c02a", "alpha tools", NO_DOMAIN, ["k@mail.example"]),
     ("billing", "c02b", "zeta works", NO_DOMAIN, ["j@mail.example"]),
     [], 0, "none"),
    ("same domain only",
     ("crm", "c03a", "alpha tools", "alpha.example", []),
     ("billing", "c03b", "zeta works", "alpha.example", []),
     ["domain"], 30, None),
    ("no domain on either side is not a shared domain",
     ("crm", "c04a", "alpha tools", NO_DOMAIN, []),
     ("billing", "c04b", "zeta works", NO_DOMAIN, []),
     [], 0, "none"),
    ("equal names also share every token",
     ("crm", "c05a", "alpha tools", NO_DOMAIN, []),
     ("billing", "c05b", "alpha tools", NO_DOMAIN, []),
     ["name_equal", "jaccard"], 60, "auto"),
    ("empty names are not equal",
     ("gl_name", "c06a", "", NO_DOMAIN, []),
     ("gl_name", "c06b", "", NO_DOMAIN, []),
     [], 0, "none"),
    ("jaccard exactly at the threshold",
     ("crm", "c07a", "a b c d e", NO_DOMAIN, []),
     ("billing", "c07b", "a b c d", NO_DOMAIN, []),
     ["jaccard"], 20, "none"),
    ("jaccard below the threshold",
     ("crm", "c08a", "a b c d", NO_DOMAIN, []),
     ("billing", "c08b", "a b c", NO_DOMAIN, []),
     [], 0, "none"),
    ("domain and jaccard land in the doubt band: reviewed same",
     ("crm", "c09a", "a b c d e", "nine.example", []),
     ("billing", "c09b", "a b c d", "nine.example", []),
     ["domain", "jaccard"], 50, "same"),
    ("domain and jaccard land in the doubt band: reviewed different, listed the other way round",
     ("crm", "c10a", "a b c d e", "ten.example", []),
     ("billing", "c10b", "a b c d", "ten.example", []),
     ["domain", "jaccard"], 50, "different"),
    ("every rule but the remittance",
     ("crm", "c11a", "alpha tools", "alpha.example", ["k@alpha.example"]),
     ("billing", "c11b", "alpha tools", "alpha.example", ["k@alpha.example"]),
     ["email_exact", "domain", "name_equal", "jaccard"], 150, "auto"),
    ("a bank reference naming the other record's invoice",
     ("bank_name", "Zeta Works Holding", "zeta works holding", NO_DOMAIN, []),
     ("billing", "c12b", "zeta works", NO_DOMAIN, []),
     ["remittance_ref"], 60, "auto"),
    ("a bank reference naming another customer's invoice",
     ("bank_name", "Zeta Works Holding", "zeta works holding", NO_DOMAIN, []),
     ("billing", "c13b", "zeta works", NO_DOMAIN, []),
     [], 0, "none"),
]
REVIEWS = {"c09a": ("c09b", "same"), "c10b": ("c10a", "different")}

def variant_row(variant):
    system, source_id, norm, domain, emails = variant
    return (system, source_id, source_id, norm, norm.split(), domain, emails)


@pytest.fixture(scope="module")
def truth_table(tmp_path_factory) -> dict[frozenset, tuple]:
    def build(directory: Path) -> None:
        warehouse = directory / "w.duckdb"
        con = db.connect(warehouse)
        try:
            db.init_schemas(con)
            con.execute("CREATE SCHEMA IF NOT EXISTS int")
            con.execute("CREATE TABLE int.int_match_variants (source_system VARCHAR, source_id VARCHAR,"
                        " raw_name VARCHAR, norm_name VARCHAR, tokens VARCHAR[], domain VARCHAR,"
                        " contact_emails VARCHAR[])")
            con.execute("CREATE TABLE int.int_match_candidates (system_a VARCHAR, source_a VARCHAR,"
                        " system_b VARCHAR, source_b VARCHAR, block_reason VARCHAR)")
            seen = set()
            for _, a, b, *_ in CASES:
                for v in (a, b):
                    if (v[0], v[1]) not in seen:
                        seen.add((v[0], v[1]))
                        con.execute("INSERT INTO int.int_match_variants VALUES (?, ?, ?, ?, ?, ?, ?)",
                                    list(variant_row(v)))
                key = lambda v: (v[0], v[1])  # noqa: E731 - the candidate order is (system, id)
                first, second = sorted((a, b), key=key)
                con.execute("INSERT INTO int.int_match_candidates VALUES (?, ?, ?, ?, 'name_token')",
                            [first[0], first[1], second[0], second[1]])
            # the bank variant of the last two cases names an invoice of c12b only
            con.execute("CREATE SCHEMA IF NOT EXISTS raw")
            con.execute("CREATE TABLE raw.bank_transactions AS SELECT * FROM (VALUES"
                        " ('Zeta Works Holding', 'PMT inv_000012')) AS t(counterparty_text, reference_text)")
            con.execute("CREATE TABLE int.stg_invoices AS SELECT * FROM (VALUES"
                        " ('inv_000012', 'c12b'), ('inv_000013', 'c13b')) AS t(invoice_id, billing_customer_id)")
        finally:
            con.close()
        pairs = [{"a": a, "b": b, "decision": d, "reason": "handmade review"} for a, (b, d) in REVIEWS.items()]
        score(warehouse, scratch_config(directory / "config", pairs))

    return rows_by_pair(shared_dir(tmp_path_factory, "scoring-truth-table", build) / "w.duckdb")


@pytest.mark.parametrize("name, a, b, rules, points, decision", CASES, ids=[c[0] for c in CASES])
def test_truth_table(truth_table, name, a, b, rules, points, decision):
    got = truth_table[frozenset((a[:2], b[:2]))]
    assert got == (points, rules, decision)


def test_one_row_per_candidate_pair(truth_table):
    assert len(truth_table) == len(CASES)


# ------------------------------------------------------------------ the mini books


def reviews_of_designed_pairs() -> list[dict]:
    return [{"a": a, "b": b, "decision": d, "reason": "designed pair"} for a, b, d in designed_pairs()]


@pytest.fixture(scope="session")
def mini_scored(tmp_path_factory, mini_loaded) -> Path:  # noqa: F811
    """The `mini` warehouse with the scoring model built over the designed pairs' reviews."""

    def build(directory: Path) -> None:
        shutil.copy(mini_loaded, directory / "w.duckdb")
        score(directory / "w.duckdb", scratch_config(directory / "config", reviews_of_designed_pairs()),
              select="+fct_match_candidates")

    return shared_dir(tmp_path_factory, "scoring-mini", build) / "w.duckdb"


def test_the_designed_doubt_pairs_score_in_the_doubt_band_with_their_reviewed_decision(mini_scored):
    got = rows_by_pair(mini_scored)
    assert designed_pairs()
    for a, b, decision in designed_pairs():
        points, rules, reviewed = got[frozenset((variant_of(a), variant_of(b)))]
        assert 30 <= points < 60, f"{a} / {b} scored {points}"
        assert rules, f"{a} / {b} has no evidence"
        assert reviewed == decision


def test_only_the_designed_pairs_are_in_the_doubt_band(mini_scored):
    designed = {frozenset((variant_of(a), variant_of(b))) for a, b, _ in designed_pairs()}
    doubt = {pair for pair, (points, _, _) in rows_by_pair(mini_scored).items() if 30 <= points < 60}
    assert doubt == designed


def identities() -> dict[tuple[str, str], str]:
    systems = {"crm", "billing", "bank_name", "gl_name"}
    with (MINI_TRUTH / "identities.csv").open(encoding="utf-8", newline="") as fh:
        return {(r["source_system"], r["source_id"]): r["true_customer_id"]
                for r in csv.DictReader(fh) if r["source_system"] in systems}


def components(edges: list[tuple], nodes: set[tuple]) -> dict[tuple, tuple]:
    parent = {n: n for n in nodes}

    def find(n: tuple) -> tuple:
        while parent[n] != n:
            parent[n] = parent[parent[n]]
            n = parent[n]
        return n

    for a, b in edges:
        parent[find(a)] = find(b)
    return {n: find(n) for n in nodes}


def test_every_variant_of_a_customer_is_joined_by_pairs_scoring_60_or_more(mini_scored):
    owner = identities()
    auto = [tuple(sorted(pair)) for pair, (points, _, _) in rows_by_pair(mini_scored).items() if points >= 60]
    merged = components(auto, set(owner) | {v for edge in auto for v in edge})
    for customer in set(owner.values()):
        roots = {merged[v] for v, c in owner.items() if c == customer}
        assert len(roots) == 1, f"{customer} splits into {len(roots)} groups by automatic pairs"


def test_no_automatic_pair_joins_two_different_customers(mini_scored):
    owner = identities()
    for pair, (points, rules, decision) in rows_by_pair(mini_scored).items():
        if decision == "auto":
            a, b = sorted(pair)
            if a in owner or b in owner:  # a payee that is no customer may merge with its own spellings
                assert owner.get(a) == owner.get(b), f"{a} / {b} merged automatically with {rules}"


def test_a_changed_weight_changes_the_scores(tmp_path, mini_scored):
    baseline = rows_by_pair(mini_scored)
    warehouse = tmp_path / "w.duckdb"
    shutil.copy(mini_scored, warehouse)
    # a lighter shared-website rule pushes the designed pairs below the doubt band
    score(warehouse, scratch_config(tmp_path / "config", reviews_of_designed_pairs(), {"domain": 10}))
    changed = rows_by_pair(warehouse)
    assert changed.keys() == baseline.keys()
    for a, b, _ in designed_pairs():
        pair = frozenset((variant_of(a), variant_of(b)))
        assert baseline[pair][0] == 30 and changed[pair][0] == 10
        assert changed[pair][2] == "none"
    unchanged = {p for p, row in baseline.items() if "domain" not in row[1]}
    assert unchanged and all(changed[p] == baseline[p] for p in unchanged)


# ------------------------------------------------------------------ the configuration in the warehouse


def test_the_configuration_lands_in_raw_tables(tmp_path):
    pairs = [{"a": "acct_0001", "b": "bill_0001", "decision": "same", "reason": "one customer"}]
    config = scratch_config(tmp_path / "config", pairs, {"domain": 25})
    con = db.connect(tmp_path / "w.duckdb")
    try:
        counts = load.load_config(con, config)
        policy = dict(con.execute("SELECT key, value FROM raw.ref_policy").fetchall())
        matches = con.execute("SELECT a, b, decision, reason, length(_row_hash) FROM raw.ref_customer_matches").fetchall()
    finally:
        con.close()
    assert counts == {"ref_policy": len(policy), "ref_customer_matches": 1}
    assert policy["match_weights.domain"] == 25 and policy["auto_match_min"] == 60
    assert policy["aging_buckets_days.3"] == 90 and policy["cases.downside.pipeline_shift_days"] == 30
    assert matches == [("acct_0001", "bill_0001", "same", "one customer", 64)]
