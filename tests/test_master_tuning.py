"""The matching section of the policy, measured against the answer key on the full books.

Marked `slow`: the full 50-customer books are generated and matched. The reviewed decisions are
the answer key's, so the check sees the automatic matcher alone; the committed matches file lists
the same doubt pairs for the reviewer and decides none of them."""

from __future__ import annotations

import csv
import shutil

import pytest
import yaml

from c2c import gen, policy
from c2c.gen import manifest
from conftest import ROOT, c2c, signed_config

pytestmark = pytest.mark.slow

SEED = 20261014
MONTH = "2026-09"
PRECISION_AUTO = 10000
RECALL_AUTO_MIN = 8500
DOUBT_PAIRS = range(10, 31)
CONFIG = ROOT / "config"


@pytest.fixture(scope="module")
def checked(tmp_path_factory):
    """`master --check` over the full books, with a signed scratch copy of `config/` whose
    reviewed matches are the answer key's decisions. Returns the report and the answer key."""
    root = tmp_path_factory.mktemp("tuning")
    gen.generate("full", SEED, root / "data")
    books, truth = manifest.preset_dirs(root / "data", "full")
    with (truth / "matches_expected.csv").open(encoding="utf-8", newline="") as fh:
        expected = list(csv.DictReader(fh))

    source = root / "source"
    shutil.copytree(CONFIG, source, ignore=shutil.ignore_patterns("signoff"))
    pairs = [{"a": r["source_a"], "b": r["source_b"], "decision": r["decision"], "reason": "answer key"}
             for r in expected]
    (source / "customer_matches.yaml").write_text(yaml.safe_dump({"pairs": pairs}, sort_keys=False))
    config_dir, signoff_dir = signed_config(root, source)

    done = c2c("master", "--month", MONTH, "--check", "--books", str(books), "--truth", str(truth),
               "--warehouse", str(root / "w.duckdb"), "--config", str(config_dir), "--json",
               env={"C2C_SIGNOFF_DIR": str(signoff_dir)})
    assert done.code == 0, done.stdout + done.stderr
    return done.json, expected


def test_the_automatic_matcher_never_merges_two_different_customers(checked):
    report, _ = checked
    assert report["scores"]["precision_auto"] == PRECISION_AUTO


def test_the_automatic_matcher_finds_most_of_the_true_pairs(checked):
    report, _ = checked
    assert report["scores"]["recall_auto"] >= RECALL_AUTO_MIN


def test_the_doubt_band_is_neither_empty_nor_a_review_burden(checked):
    report, expected = checked
    assert report["scores"]["pairs_doubt"] in DOUBT_PAIRS
    assert report["scores"]["pairs_doubt"] == report["scores"]["pairs_reviewed"] == len(expected)


def test_review_leaves_nothing_wrong(checked):
    report, _ = checked
    assert report["scores"]["precision_reviewed"] == report["scores"]["recall_reviewed"] == 10000


def test_the_committed_matches_list_every_doubt_pair_undecided(checked):
    report, expected = checked
    p = policy.load(CONFIG)
    band = {frozenset((c["a"]["id"], c["b"]["id"])) for c in report["candidates"]
            if p.doubt_min <= c["score"] < p.auto_match_min}
    document = yaml.safe_load((CONFIG / "customer_matches.yaml").read_text())
    listed = document["pairs"]
    assert {frozenset((e["a"], e["b"])) for e in listed} == band
    assert band == {frozenset((r["source_a"], r["source_b"])) for r in expected}
    assert len(listed) == len(band)
    for entry in listed:
        assert entry["decision"] is None and entry["reason"] == ""
    # the loader refuses the file until the reviewer fills every decision in
    with pytest.raises(policy.PolicyError, match="decision"):
        policy.load_matches(CONFIG)
