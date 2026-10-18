"""The seeded entity plan: determinism, key-order independence and the planted roles."""

from __future__ import annotations

import copy
import csv
import re

import pytest

from c2c.gen import plan as plan_mod
from c2c.gen.plan import INJECT_ERRORS, SHAPE_ROLES, build_plan, load_config
from c2c.gen.seeds import rng

ROOT = plan_mod.ROOT
MINI_ROLES = set(SHAPE_ROLES) | {"doubt_pair_same", "doubt_pair_different", "doubt_lookalike"} | {
    f"inject_{error}" for error in INJECT_ERRORS}


@pytest.fixture(scope="module")
def cfg():
    return load_config()


def _reversed_keys(node):
    if isinstance(node, dict):
        return {k: _reversed_keys(v) for k, v in reversed(list(node.items()))}
    if isinstance(node, list):
        return [_reversed_keys(v) for v in node]
    return node


def _read_csv(name):
    with (ROOT / "data" / "reference" / name).open(encoding="utf-8", newline="") as fh:
        return list(csv.DictReader(fh))


def test_rng_streams_are_keyed_and_repeatable():
    assert rng(1, "acct_0001", "x").random() == rng(1, "acct_0001", "x").random()
    assert rng(1, "acct_0001", "x").random() != rng(1, "acct_0001", "y").random()
    assert rng(1, "acct_0001", "x").random() != rng(1, "acct_0002", "x").random()
    assert rng(1, "acct_0001", "x").random() != rng(2, "acct_0001", "x").random()


@pytest.mark.parametrize("preset", ["small", "mini", "full"])
def test_two_builds_are_byte_identical(cfg, preset):
    first = build_plan(preset, cfg, cfg["seed"]).to_json().encode()
    second = build_plan(preset, copy.deepcopy(cfg), cfg["seed"]).to_json().encode()
    assert first == second


@pytest.mark.parametrize("preset", ["small", "mini", "full"])
def test_reordering_config_keys_changes_nothing(cfg, preset):
    expected = build_plan(preset, cfg, cfg["seed"]).to_json()
    assert build_plan(preset, _reversed_keys(cfg), cfg["seed"]).to_json() == expected


def test_seed_changes_the_plan(cfg):
    assert build_plan("mini", cfg, 1).to_json() != build_plan("mini", cfg, 2).to_json()


def test_config_holds_only_integers(cfg):
    plan_mod._reject_floats(cfg, "config")
    assert isinstance(cfg["seed"], int) and cfg["seed"] == 20261014
    assert cfg["company"] == "Northwind Labs (synthetic)"
    with pytest.raises(ValueError, match="not an integer"):
        plan_mod._reject_floats({"a": [{"b": 0.5}]}, "config")


def test_unknown_preset_is_refused(cfg):
    with pytest.raises(ValueError, match="unknown preset"):
        build_plan("huge", cfg, cfg["seed"])


def test_small_has_exactly_three_customers_and_no_planted_shapes(cfg):
    plan = build_plan("small", cfg, cfg["seed"])
    assert [c.crm_account_id for c in plan.customers] == ["acct_0001", "acct_0002", "acct_0003"]
    assert (plan.first_month, plan.last_month) == ("2026-01", "2026-03")
    assert plan.months == ("2026-01", "2026-02", "2026-03")
    assert plan.truth_months == () and plan.truth_first_month is None
    assert plan.roles == {} and plan.doubt_pairs == ()
    assert all(c.variant_count == 1 and c.roles == () and c.amendment_month == "" for c in plan.customers)
    assert plan.min_cash_hint is None


def test_mini_carries_every_planted_role_exactly_once(cfg):
    plan = build_plan("mini", cfg, cfg["seed"])
    assert len(plan.customers) == 10
    assert set(plan.roles) == MINI_ROLES
    for role, accts in plan.roles.items():
        assert len(accts) == 1, role
        carriers = [c.crm_account_id for c in plan.customers if role in c.roles]
        assert carriers == list(accts), role
    assert sorted(p.decision for p in plan.doubt_pairs) == ["different", "same"]
    by_decision = {p.decision: p for p in plan.doubt_pairs}
    assert by_decision["same"].customer == plan.roles["doubt_pair_same"][0]
    assert by_decision["same"].other == ""
    assert by_decision["different"].customer == plan.roles["doubt_pair_different"][0]
    assert by_decision["different"].other == plan.roles["doubt_lookalike"][0]
    assert isinstance(plan.min_cash_hint, int)


def test_mini_planted_roles_have_what_they_need(cfg):
    plan = build_plan("mini", cfg, cfg["seed"])
    by_id = {c.crm_account_id: c for c in plan.customers}
    assert by_id[plan.roles["four_variant"][0]].variant_count == 4
    assert by_id[plan.roles["doubt_pair_same"][0]].variant_count >= 2
    assert max(c.variant_count for c in plan.customers if "four_variant" not in c.roles) <= 3
    amended = by_id[plan.roles["amendment"][0]]
    assert amended.start_month < amended.amendment_month <= plan.last_month
    assert sum(1 for c in plan.customers if c.amendment_month) == 1
    for error in INJECT_ERRORS:
        target = by_id[plan.roles[f"inject_{error}"][0]]
        assert target.start_month <= "2026-01" and (not target.churn_month or target.churn_month > "2026-01")


def test_mini_window_and_horizon(cfg):
    plan = build_plan("mini", cfg, cfg["seed"])
    assert (plan.months[0], plan.months[-1], len(plan.months)) == ("2024-10", "2026-03", 18)
    assert plan.truth_months == ("2026-04", "2026-05", "2026-06")
    assert plan.calendar[0] >= "2024-10-01" and plan.calendar[-1] <= "2026-06-30"


def test_full_shape(cfg):
    plan = build_plan("full", cfg, cfg["seed"])
    assert len(plan.customers) == 50 and len(plan.reps) == 6
    assert (plan.months[0], plan.months[-1], len(plan.months)) == ("2023-10", "2026-09", 36)
    assert (plan.truth_months[0], plan.truth_months[-1], len(plan.truth_months)) == ("2026-10", "2027-09", 12)
    assert len(plan.doubt_pairs) == 15
    assert [c.crm_account_id for c in plan.customers] == [f"acct_{i:04d}" for i in range(1, 51)]


@pytest.mark.parametrize("preset", ["small", "mini", "full"])
def test_customers_are_well_formed(cfg, preset):
    plan = build_plan(preset, cfg, cfg["seed"])
    lookalikes = {p.other: p.customer for p in plan.doubt_pairs if p.other}
    names = [c.name for c in plan.customers]
    assert len(set(names)) == len(names)
    assert len({c.domain for c in plan.customers}) == len(plan.customers)
    first_words = [c.name.split()[0] for c in plan.customers if c.crm_account_id not in lookalikes]
    assert len(set(first_words)) == len(first_words)
    by_id = {c.crm_account_id: c for c in plan.customers}
    for other, customer in lookalikes.items():
        assert by_id[other].name.split()[0] == by_id[customer].name.split()[0]
    suffixes = {row["suffix"] for row in _read_csv("legal_suffixes.csv")}
    rep_ids = {r.rep_id for r in plan.reps}
    for c in plan.customers:
        assert re.fullmatch(r"acct_\d{4}", c.crm_account_id)
        assert c.name.split()[-1].lower() in suffixes
        assert c.owner_rep_id in rep_ids
        assert c.term_months in (12, 24, 36)
        assert c.billing_frequency in ("annual", "quarterly")
        assert c.payment_terms_days in (30, 45, 60)
        assert c.acv_cents % cfg["acv_step_cents"] == 0
        assert plan.months[0] <= c.start_month <= plan.months[-1]
        assert c.start_month > c.signing_month
        assert 1 <= c.variant_count <= cfg["presets"][preset]["max_variants"]
        assert c.dtp.median_days >= 1 and c.dtp.spread_days >= 1
        assert not c.churn_month or c.churn_month > c.start_month


def test_calendar_is_the_weekday_calendar_of_the_window(cfg):
    plan = build_plan("small", cfg, cfg["seed"])
    assert plan.calendar[0] == "2026-01-01" and plan.calendar[-1] == "2026-03-31"
    assert "2026-01-03" not in plan.calendar and "2026-01-05" in plan.calendar
    assert len(plan.calendar) == 64


def test_reference_files():
    days = _read_csv("business_days.csv")
    assert (days[0]["date"], days[-1]["date"], len(days)) == ("2023-01-01", "2027-12-31", 1826)
    from datetime import date
    assert all((date.fromisoformat(r["date"]).weekday() < 5) == (r["is_business_day"] == "1") for r in days)
    accounts = {row["gl_account"]: row for row in _read_csv("gl_accounts.csv")}
    assert sorted(accounts) == ["1000", "1200", "2400", "3000", "4000", "4100",
                                "6000", "6100", "6200", "6300", "6400", "6500"]
    assert accounts["2400"]["name"] == "Deferred revenue"
    assert {"inc", "llc", "ltd", "corp"} <= {r["suffix"] for r in _read_csv("legal_suffixes.csv")}
    freemail = {r["domain"] for r in _read_csv("freemail_domains.csv")}
    assert "gmail.com" in freemail and not any(c.endswith(".example") for c in freemail)
