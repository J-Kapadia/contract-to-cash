"""The reviewed `mini` configuration: its policy differs from the default policy only where the small
books need it, and its reviewed matches cover exactly the pairs the scorer leaves in doubt."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
import yaml

from c2c import policy
from conftest import DEFAULT_CONFIG, ROOT, shared_dir
from test_match_normalisation import designed_pairs, mini_loaded  # noqa: F401
from test_match_scoring import rows_by_pair, score

DEFAULT_POLICY = ROOT / "config"


def test_the_fixture_policy_loads_and_differs_from_the_default_only_where_the_small_books_need_it():
    fixture = yaml.safe_load((DEFAULT_CONFIG / "policy.yaml").read_text())
    default = yaml.safe_load((DEFAULT_POLICY / "policy.yaml").read_text())
    assert policy.load(DEFAULT_CONFIG).backtest_min_history_months == 12
    changed = {key for key in default.keys() | fixture.keys() if default.get(key) != fixture.get(key)}
    assert changed == {"backtest_min_history_months", "min_cash_cents"}
    assert fixture["backtest_min_history_months"] == 12
    # a multiple of 1,000,000 cents, below the lowest weekly ending balance of the 13 weeks after 2026-01-31
    assert fixture["min_cash_cents"] == 227_000_000


def test_every_reviewed_pair_has_a_decision_and_a_reason_and_none_is_listed_twice():
    matches = policy.load_matches(DEFAULT_CONFIG)
    assert matches
    assert len({frozenset((m.a, m.b)) for m in matches}) == len(matches)
    for m in matches:
        assert m.decision in ("same", "different")
        assert m.reason.strip()


@pytest.fixture(scope="module")
def scored(tmp_path_factory, mini_loaded) -> dict:  # noqa: F811
    """Score, rules and decision of every candidate pair of `mini`, scored with the fixture configuration."""

    def build(directory: Path) -> None:
        shutil.copy(mini_loaded, directory / "w.duckdb")
        score(directory / "w.duckdb", DEFAULT_CONFIG, select="+fct_match_candidates")

    return rows_by_pair(shared_dir(tmp_path_factory, "fixture-config-scored", build) / "w.duckdb")


def test_the_reviewed_pairs_are_exactly_the_doubt_band_and_follow_the_answer_key(scored):
    p = policy.load(DEFAULT_CONFIG)
    band = {frozenset(i for _, i in pair) for pair, (points, _, _) in scored.items()
            if p.doubt_min <= points < p.auto_match_min}
    listed = {frozenset((m.a, m.b)): m.decision for m in policy.load_matches(DEFAULT_CONFIG)}
    assert band
    assert set(listed) == band
    assert listed == {frozenset((a, b)): decision for a, b, decision in designed_pairs()}


def test_every_candidate_pair_in_the_doubt_band_carries_its_reviewed_decision(scored):
    p = policy.load(DEFAULT_CONFIG)
    decided = {frozenset((m.a, m.b)): m.decision for m in policy.load_matches(DEFAULT_CONFIG)}
    for pair, (points, _, decision) in scored.items():
        if p.doubt_min <= points < p.auto_match_min:
            assert decision == decided[frozenset(i for _, i in pair)]
        else:
            assert frozenset(i for _, i in pair) not in decided
