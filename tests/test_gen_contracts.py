"""Contracts, amendments and renewals: terms, versions, outcomes and determinism."""

from __future__ import annotations

import json
from collections import defaultdict
from datetime import date

import pytest

from c2c.gen.contracts import amendment_kind, contracts
from c2c.gen.plan import build_plan, load_config


@pytest.fixture(scope="module")
def cfg():
    return load_config()


@pytest.fixture(scope="module", params=["small", "mini", "full"])
def built(request, cfg):
    plan = build_plan(request.param, cfg, cfg["seed"])
    return plan, contracts(plan)


def _d(text: str) -> date:
    return date.fromisoformat(text)


def _by_contract(rows):
    out = defaultdict(list)
    for row in rows:
        out[row["contract_id"]].append(row)
    return out


def test_every_amendment_is_effective_inside_its_term(built):
    _, (rows, _, _) = built
    for row in rows:
        assert _d(row["term_start"]) <= _d(row["effective_from"]) <= _d(row["term_end"])
        if row["version"] > 1:
            assert _d(row["term_start"]) < _d(row["effective_from"]) < _d(row["term_end"])
            assert row["signed_on"] == row["effective_from"]


def test_versions_are_contiguous_and_share_the_term(built):
    _, (rows, _, _) = built
    for contract_id, versions in _by_contract(rows).items():
        versions.sort(key=lambda r: r["version"])
        assert [v["version"] for v in versions] == list(range(1, len(versions) + 1)), contract_id
        assert len({(v["crm_account_id"], v["term_start"], v["term_end"], v["renewal_number"],
                     v["billing_frequency"], v["payment_terms_days"]) for v in versions}) == 1
        effective = [v["effective_from"] for v in versions]
        assert effective == sorted(effective) and len(set(effective)) == len(effective)


def test_each_version_carries_a_subscription_and_an_overage_line(built):
    _, (rows, lines, _) = built
    keys = {(r["contract_id"], r["version"]) for r in rows}
    seen = defaultdict(list)
    for ln in lines:
        seen[(ln["contract_id"], ln["version"])].append(ln)
    assert set(seen) == keys
    for key, version_lines in seen.items():
        assert sorted(ln["product"] for ln in version_lines) == ["overage", "subscription"], key
        assert len({ln["line_id"] for ln in version_lines}) == 2
        sub = next(ln for ln in version_lines if ln["product"] == "subscription")
        ovg = next(ln for ln in version_lines if ln["product"] == "overage")
        assert sub["annual_fee_cents"] > 0 and ovg["included_units"] > 0
        assert ovg["overage_cents_per_unit"] > 0


def test_renewals_never_overlap_the_prior_term(built):
    plan, (rows, _, _) = built
    firsts = {r["contract_id"]: r for r in rows if r["version"] == 1}
    per_account = defaultdict(list)
    for row in firsts.values():
        per_account[row["crm_account_id"]].append(row)
    for terms in per_account.values():
        terms.sort(key=lambda r: r["renewal_number"])
        assert [t["renewal_number"] for t in terms] == list(range(len(terms)))
        for prior, nxt in zip(terms, terms[1:]):
            assert _d(nxt["term_start"]) > _d(prior["term_end"])
            assert (_d(nxt["term_start"]) - _d(prior["term_end"])).days == 1
            assert _d(nxt["signed_on"]) < _d(nxt["term_start"])
    for customer in plan.customers:
        assert len(per_account[customer.crm_account_id]) == customer.renewal_count + 1


def test_rows_stay_in_the_books_window(built):
    plan, (rows, _, _) = built
    for row in rows:
        assert row["term_start"][:7] <= plan.last_month
        assert row["signed_on"][:7] <= plan.last_month


def test_mini_has_exactly_one_mid_term_amendment(cfg):
    plan = build_plan("mini", cfg, cfg["seed"])
    rows, lines, _ = contracts(plan)
    amended = [r for r in rows if r["version"] > 1]
    assert len(amended) == 1
    (row,) = amended
    (acct,) = plan.roles["amendment"]
    assert row["crm_account_id"] == acct
    assert row["effective_from"][:7] == next(c for c in plan.customers if c.crm_account_id == acct).amendment_month
    kind = amendment_kind(rows, lines, row["contract_id"], row["version"])
    assert kind in {"upgrade", "downgrade"}
    fee = {ln["version"]: ln["annual_fee_cents"] for ln in lines
           if ln["contract_id"] == row["contract_id"] and ln["product"] == "subscription"}
    assert (fee[2] > fee[1]) == (kind == "upgrade")


def test_amendment_kind_rejects_the_first_version(cfg):
    plan = build_plan("mini", cfg, cfg["seed"])
    rows, lines, _ = contracts(plan)
    with pytest.raises(ValueError):
        amendment_kind(rows, lines, rows[0]["contract_id"], 1)


def test_outcomes_follow_the_plan(built):
    plan, (rows, lines, outcomes) = built
    first_fee = {}
    last_fee = {}
    for ln in lines:
        if ln["product"] == "subscription":
            first_fee.setdefault(ln["contract_id"], ln["annual_fee_cents"])
            last_fee[ln["contract_id"]] = ln["annual_fee_cents"]
    account = {r["contract_id"]: r["crm_account_id"] for r in rows}
    by_customer = {c.crm_account_id: c for c in plan.customers}
    assert outcomes or plan.preset == "small"  # three months hold no expiry
    for o in outcomes:
        customer = by_customer[account[o["contract_id"]]]
        assert o["outcome"] in {"renewed", "churned", "expanded"}
        assert o["acv_before"] == last_fee[o["contract_id"]]
        expiry_month = _month_after(o["expiry"])
        if o["outcome"] == "churned":
            assert customer.churn_month == expiry_month and o["acv_after"] == 0
        elif o["outcome"] == "renewed":
            assert o["acv_after"] == o["acv_before"]
            assert expiry_month not in {e.month for e in customer.expansion_plan}
        else:
            uplift = {e.month: e.uplift_bps for e in customer.expansion_plan}[expiry_month]
            assert o["acv_after"] == o["acv_before"] * (10_000 + uplift) // 10_000 > o["acv_before"]
    # a renewal that is in the books starts from the fee its outcome gives
    for row in rows:
        if row["version"] == 1 and row["renewal_number"] > 0:
            prior = next(o for o in outcomes if o["contract_id"] == _prior_id(row["contract_id"]))
            assert first_fee[row["contract_id"]] == prior["acv_after"]
    churned = {account[o["contract_id"]] for o in outcomes if o["outcome"] == "churned"}
    assert churned == {c.crm_account_id for c in plan.customers if c.churn_month}


def _month_after(day: str) -> str:
    year, month = int(day[:4]), int(day[5:7])
    return f"{year + month // 12:04d}-{month % 12 + 1:02d}"


def _prior_id(contract_id: str) -> str:
    stem, _, num = contract_id.rpartition("_")
    return f"{stem}_{int(num) - 1:02d}"


def test_full_books_cluster_in_the_fourth_and_first_quarters(cfg):
    plan = build_plan("full", cfg, cfg["seed"])
    rows, _, _ = contracts(plan)
    firsts = [r for r in rows if r["version"] == 1 and r["renewal_number"] == 0]
    renewals = [r for r in rows if r["version"] == 1 and r["renewal_number"] > 0]
    assert renewals
    for group in (firsts, renewals):
        hot = sum(1 for r in group if int(r["signed_on"][5:7]) in {10, 11, 12, 1, 2, 3})
        assert hot * 100 >= 60 * len(group)


def test_two_calls_are_identical(cfg):
    for preset in ("small", "mini", "full"):
        plan = build_plan(preset, cfg, cfg["seed"])
        first, second = contracts(plan), contracts(build_plan(preset, cfg, cfg["seed"]))
        assert json.dumps(first, sort_keys=True) == json.dumps(second, sort_keys=True)
