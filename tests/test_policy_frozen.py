"""Only the matching section of the policy is tuned: every other value stays at the frozen constant."""

from __future__ import annotations

from pathlib import Path

import yaml

from conftest import DEFAULT_CONFIG, ROOT

CONSTANTS = ROOT / "tests" / "fixtures" / "policy_constants.yaml"
MATCHING_KEYS = {"auto_match_min", "doubt_min", "match_weights", "jaccard_min_bps"}
# The two values the `mini` books need their own copy of.
FIXTURE_KEYS = {"backtest_min_history_months", "min_cash_cents"}


def read(path: Path) -> dict:
    return yaml.safe_load(path.read_text())


def test_the_constants_name_no_matching_key():
    assert not MATCHING_KEYS & set(read(CONSTANTS))


def test_the_policy_holds_the_constants_and_the_matching_section_only():
    policy = read(ROOT / "config" / "policy.yaml")
    assert set(policy) == set(read(CONSTANTS)) | MATCHING_KEYS


def test_the_policy_equals_the_constants_outside_the_matching_keys():
    policy = read(ROOT / "config" / "policy.yaml")
    frozen = read(CONSTANTS)
    assert {k: v for k, v in policy.items() if k not in MATCHING_KEYS} == frozen


def test_the_fixture_policy_equals_the_constants_outside_the_matching_and_fixture_keys():
    fixture = read(DEFAULT_CONFIG / "policy.yaml")
    frozen = read(CONSTANTS)
    assert set(fixture) == set(frozen) | MATCHING_KEYS
    skipped = MATCHING_KEYS | FIXTURE_KEYS
    assert {k: v for k, v in fixture.items() if k not in skipped} == {
        k: v for k, v in frozen.items() if k not in skipped}
