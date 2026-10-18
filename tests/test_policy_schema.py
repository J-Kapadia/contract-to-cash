"""The committed policy and matches files load, and the loader rejects anything off-schema."""

from __future__ import annotations

import copy
import hashlib
from pathlib import Path

import pytest
import yaml

from c2c import policy
from c2c.errors import UsageError

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "config"


@pytest.fixture
def raw() -> dict:
    return yaml.safe_load((CONFIG / "policy.yaml").read_text())


def write(tmp_path: Path, document: dict) -> Path:
    (tmp_path / "policy.yaml").write_text(yaml.safe_dump(document))
    return tmp_path


def rejected(tmp_path: Path, document: dict, *words: str) -> None:
    with pytest.raises(policy.PolicyError) as caught:
        policy.load(write(tmp_path, document))
    for word in words:
        assert word in str(caught.value)
    assert caught.value.exit == 2


# ------------------------------------------------------------------ the committed files


def test_the_committed_policy_loads_at_its_defaults():
    p = policy.load(CONFIG)
    assert (p.auto_match_min, p.doubt_min) == (60, 30)
    assert p.match_weights == policy.MatchWeights(60, 30, 40, 20, 60)
    assert p.jaccard_min_bps == 8000
    assert p.aging_buckets_days == (0, 30, 60, 90)
    assert (p.dtp_window_months, p.dtp_min_paid_invoices, p.min_observations) == (24, 3, 8)
    assert (p.at_risk_days_past_due, p.stale_opp_shift_days, p.stale_opp_weight_bps) == (120, 28, 5000)
    assert (p.cohort_window_months, p.stage_win_window_months) == (24, 24)
    assert p.stage_defaults_bps == policy.StageDefaults(1000, 2000, 4000, 6000, 8000)
    assert (p.min_cash_cents, p.escalate_cents, p.escalate_days_past_due) == (150000000, 5000000, 60)
    assert (p.renewal_review_days, p.renewal_review_acv_percentile, p.driver_review_bps) == (90, 75, 1000)
    assert (p.backtest_min_history_months, p.nrr_window_months) == (18, 12)


def test_the_committed_cases_follow_the_driver_table():
    cases = policy.load(CONFIG).cases
    assert (cases.base.renewal_down_bps, cases.base.renewal_up_bps) == (0, 0)
    assert cases.downside.renewal_down_bps == 1000 and cases.upside.renewal_up_bps == 500
    assert (cases.base.expansion_mult_bps, cases.downside.expansion_mult_bps, cases.upside.expansion_mult_bps) == (
        10000, 0, 12500)
    assert (cases.downside.pipeline_weight_mult_bps, cases.downside.pipeline_shift_days) == (5000, 30)
    assert cases.upside.pipeline_weight_mult_bps == 12500
    assert (cases.base.collections_percentile, cases.downside.collections_percentile) == (50, 80)
    assert cases.upside.collections_cap_at_terms == 1 and cases.base.collections_cap_at_terms == 0
    assert cases.downside.cloud_up_bps == 1000 and cases.base.cloud_up_bps == 0
    assert {c.cloud_trailing_months for c in (cases.base, cases.downside, cases.upside)} == {3}


def test_the_committed_matches_start_empty():
    assert policy.load_matches(CONFIG) == ()
    assert yaml.safe_load((CONFIG / "customer_matches.yaml").read_text()) == {"pairs": []}


def test_sha256_of_hashes_the_file_bytes():
    assert policy.sha256_of(CONFIG / "policy.yaml") == hashlib.sha256((CONFIG / "policy.yaml").read_bytes()).hexdigest()


def test_a_policy_error_is_a_usage_error_with_exit_2():
    assert issubclass(policy.PolicyError, UsageError) and policy.PolicyError("x").exit == 2


# ------------------------------------------------------------------ the policy is rejected when off-schema


def test_an_unknown_key_is_rejected_by_name(raw, tmp_path):
    raw["surprise_days"] = 5
    rejected(tmp_path, raw, "unknown", "surprise_days")


def test_a_missing_key_is_rejected_by_name(raw, tmp_path):
    del raw["min_cash_cents"]
    rejected(tmp_path, raw, "missing", "min_cash_cents")


def test_every_top_level_key_is_required(raw, tmp_path):
    for key in list(raw):
        trimmed = {k: v for k, v in raw.items() if k != key}
        rejected(tmp_path, trimmed, "missing", key)


def test_an_unknown_or_missing_nested_key_is_rejected_by_path(raw, tmp_path):
    extra = copy.deepcopy(raw)
    extra["match_weights"]["phone"] = 10
    rejected(tmp_path, extra, "unknown", "match_weights.phone")
    short = copy.deepcopy(raw)
    del short["stage_defaults_bps"]["S3"]
    rejected(tmp_path, short, "missing", "stage_defaults_bps.S3")
    gap = copy.deepcopy(raw)
    del gap["cases"]["base"]["cloud_trailing_months"]
    rejected(tmp_path, gap, "missing", "cases.base.cloud_trailing_months")


def test_a_float_is_rejected(raw, tmp_path):
    raw["jaccard_min_bps"] = 8000.0
    rejected(tmp_path, raw, "jaccard_min_bps", "integer")
    raw["jaccard_min_bps"] = 8000
    raw["cases"]["upside"]["expansion_mult_bps"] = 12500.5
    rejected(tmp_path, raw, "cases.upside.expansion_mult_bps", "integer")


def test_a_bool_or_string_is_not_an_integer(raw, tmp_path):
    raw["min_observations"] = True
    rejected(tmp_path, raw, "min_observations", "integer")
    raw["min_observations"] = "8"
    rejected(tmp_path, raw, "min_observations", "integer")


def test_a_negative_bps_is_rejected(raw, tmp_path):
    raw["stale_opp_weight_bps"] = -1
    rejected(tmp_path, raw, "stale_opp_weight_bps", "negative")
    raw["stale_opp_weight_bps"] = 5000
    raw["cases"]["downside"]["renewal_down_bps"] = -1000
    rejected(tmp_path, raw, "cases.downside.renewal_down_bps", "negative")


def test_a_share_above_100_percent_is_rejected(raw, tmp_path):
    raw["driver_review_bps"] = 10001
    rejected(tmp_path, raw, "driver_review_bps", "10000")


def test_a_multiplier_may_exceed_100_percent(raw, tmp_path):
    raw["cases"]["upside"]["pipeline_weight_mult_bps"] = 20000
    assert policy.load(write(tmp_path, raw)).cases.upside.pipeline_weight_mult_bps == 20000


def test_cases_carry_exactly_base_downside_upside(raw, tmp_path):
    assert list(raw["cases"]) == ["base", "downside", "upside"]
    extra = copy.deepcopy(raw)
    extra["cases"]["stress"] = copy.deepcopy(extra["cases"]["base"])
    rejected(tmp_path, extra, "unknown", "cases.stress")
    missing = copy.deepcopy(raw)
    del missing["cases"]["upside"]
    rejected(tmp_path, missing, "missing", "cases.upside")


def test_buckets_must_be_ascending_integers(raw, tmp_path):
    raw["aging_buckets_days"] = [0, 60, 30, 90]
    rejected(tmp_path, raw, "aging_buckets_days", "ascending")
    raw["aging_buckets_days"] = []
    rejected(tmp_path, raw, "aging_buckets_days", "list")


def test_doubt_min_may_not_exceed_auto_match_min(raw, tmp_path):
    raw["doubt_min"] = 61
    rejected(tmp_path, raw, "doubt_min", "auto_match_min")


def test_a_missing_or_invalid_file_is_a_policy_error(tmp_path):
    with pytest.raises(policy.PolicyError, match="cannot read"):
        policy.load(tmp_path)
    (tmp_path / "policy.yaml").write_text("a: [unclosed")
    with pytest.raises(policy.PolicyError, match="not valid YAML"):
        policy.load(tmp_path)
    (tmp_path / "policy.yaml").write_text("- just\n- a list\n")
    with pytest.raises(policy.PolicyError, match="mapping"):
        policy.load(tmp_path)


# ------------------------------------------------------------------ the reviewed matches


def matches(tmp_path: Path, pairs) -> Path:
    (tmp_path / "customer_matches.yaml").write_text(yaml.safe_dump({"pairs": pairs}))
    return tmp_path


def test_matches_load_in_file_order(tmp_path):
    pairs = [{"a": "crm-1", "b": "bill-9", "decision": "same", "reason": "same remit-to address"},
             {"a": "crm-2", "b": "bill-3", "decision": "different", "reason": "separate subsidiaries"}]
    assert policy.load_matches(matches(tmp_path, pairs)) == (
        policy.Match("crm-1", "bill-9", "same", "same remit-to address"),
        policy.Match("crm-2", "bill-3", "different", "separate subsidiaries"))


@pytest.mark.parametrize("decision", [None, "", "maybe", "Same"])
def test_a_pair_without_a_valid_decision_is_rejected_naming_the_pair(tmp_path, decision):
    pairs = [{"a": "crm-1", "b": "bill-9", "decision": decision, "reason": "r"}]
    with pytest.raises(policy.PolicyError) as caught:
        policy.load_matches(matches(tmp_path, pairs))
    assert "crm-1 / bill-9" in str(caught.value) and "decision" in str(caught.value)


def test_a_pair_needs_a_reason_and_known_keys_only(tmp_path):
    with pytest.raises(policy.PolicyError, match="crm-1 / bill-9.*reason"):
        policy.load_matches(matches(tmp_path, [{"a": "crm-1", "b": "bill-9", "decision": "same", "reason": " "}]))
    with pytest.raises(policy.PolicyError, match="unknown key note"):
        policy.load_matches(matches(tmp_path, [
            {"a": "crm-1", "b": "bill-9", "decision": "same", "reason": "r", "note": "x"}]))


def test_a_pair_listed_twice_or_with_itself_is_rejected(tmp_path):
    one = {"a": "crm-1", "b": "bill-9", "decision": "same", "reason": "r"}
    flipped = {"a": "bill-9", "b": "crm-1", "decision": "different", "reason": "r"}
    with pytest.raises(policy.PolicyError, match="listed twice"):
        policy.load_matches(matches(tmp_path, [one, flipped]))
    with pytest.raises(policy.PolicyError, match="itself"):
        policy.load_matches(matches(tmp_path, [{**one, "b": "crm-1"}]))


def test_the_matches_file_holds_only_pairs(tmp_path):
    (tmp_path / "customer_matches.yaml").write_text("pairs: []\nextra: 1\n")
    with pytest.raises(policy.PolicyError, match="exactly one key"):
        policy.load_matches(tmp_path)
