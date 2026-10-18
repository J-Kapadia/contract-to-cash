"""Monthly pipeline snapshots: every contract's opportunity, stage order, stale and determinism."""

from __future__ import annotations

import calendar
import json
from collections import defaultdict
from datetime import date

import pytest

from c2c.gen.contracts import contracts
from c2c.gen.pipeline import PIPELINE_COLUMNS, STAGES, pipeline
from c2c.gen.plan import build_plan, load_config
from c2c.gen.seeds import rng

ORDER = {stage: i for i, stage in enumerate((*STAGES, "won"))}


@pytest.fixture(scope="module")
def cfg():
    return load_config()


@pytest.fixture(scope="module", params=["small", "mini", "full"])
def built(request, cfg):
    plan = build_plan(request.param, cfg, cfg["seed"])
    rows, lines, _ = contracts(plan, rng)
    return plan, rows, lines, pipeline(plan, rows, rng, lines)


@pytest.fixture(scope="module")
def mini(cfg):
    plan = build_plan("mini", cfg, cfg["seed"])
    rows, lines, _ = contracts(plan, rng)
    return plan, rows, lines, pipeline(plan, rows, rng, lines)


def _d(text: str) -> date:
    return date.fromisoformat(text)


def _by_opp(snapshots):
    out = defaultdict(list)
    for row in snapshots:
        out[row["opp_id"]].append(row)
    return out


def _month_end(d: date) -> date:
    return d.replace(day=calendar.monthrange(d.year, d.month)[1])


def test_rows_are_shaped_like_the_books(built):
    plan, _, _, snapshots = built
    assert snapshots
    last = max(_d(r["snapshot_date"]) for r in snapshots)
    assert last <= _month_end(_d(f"{plan.last_month}-01"))
    for row in snapshots:
        assert tuple(row) == PIPELINE_COLUMNS
        d = _d(row["snapshot_date"])
        assert d == _month_end(d)
        assert row["known_on"] == row["snapshot_date"]
        assert row["opp_type"] in {"new", "expansion", "renewal"}
        assert row["stage"] in ORDER or row["stage"] == "lost"
        assert isinstance(row["acv_cents"], int) and row["acv_cents"] > 0
        assert row["billing_frequency"] in {"annual", "quarterly"}
    assert len({(r["snapshot_date"], r["opp_id"]) for r in snapshots}) == len(snapshots)


def test_every_contract_has_a_preceding_won_opportunity(built):
    _, rows, lines, snapshots = built
    opps = _by_opp(snapshots)
    fee = {(ln["contract_id"], ln["version"]): ln["annual_fee_cents"] for ln in lines if ln["product"] == "subscription"}
    firsts = [r for r in rows if r["version"] == 1]
    assert firsts
    for contract in firsts:
        opp = opps["opp_" + contract["contract_id"].removeprefix("ctr_")]
        won = [r for r in opp if r["stage"] == "won"]
        assert len(won) == 1, contract["contract_id"]
        won = won[0]
        signed = _d(contract["signed_on"])
        assert won["snapshot_date"] == _month_end(signed).isoformat()
        assert won["expected_close_date"] == contract["signed_on"]
        assert won["opp_type"] == ("new" if contract["renewal_number"] == 0 else "renewal")
        assert won["crm_account_id"] == contract["crm_account_id"]
        assert won["billing_frequency"] == contract["billing_frequency"]
        assert won["acv_cents"] == fee[(contract["contract_id"], 1)]
        assert [r["stage"] for r in opp if r is not won] == [r["stage"] for r in opp if r is not won and r["stage"] in STAGES]
        before = [r for r in opp if r["stage"] in STAGES]
        assert {r["stage"] for r in before} == set(STAGES)
        assert all(_d(r["snapshot_date"]) < signed for r in before)


def test_stages_never_move_backwards(built):
    _, _, _, snapshots = built
    for opp_id, history in _by_opp(snapshots).items():
        history.sort(key=lambda r: r["snapshot_date"])
        ranks = [ORDER.get(r["stage"], 99) for r in history]
        assert ranks == sorted(ranks), opp_id
        closed = [r for r in history if r["stage"] in {"won", "lost"}]
        assert len(closed) <= 1 and (not closed or closed[0] is history[-1]), opp_id
        months = [_d(r["snapshot_date"]) for r in history]
        assert len(set(months)) == len(months)
        assert all(a.month % 12 + 1 == b.month for a, b in zip(months, months[1:])), opp_id


def test_lost_opportunities_follow_the_stage_default_rates(mini):
    _, rows, _, snapshots = mini
    opps = _by_opp(snapshots)
    won = sum(1 for r in rows if r["version"] == 1)
    seen = {s: 0 for s in STAGES}
    reached_won = 0
    for history in opps.values():
        stages = {r["stage"] for r in history}
        if "lost" not in stages and "won" not in stages:
            continue
        for s in STAGES:
            seen[s] += s in stages
        reached_won += "won" in stages
    assert reached_won == won
    for s, bps in zip(STAGES, (1000, 2000, 4000, 6000, 8000)):
        assert abs(won * 10_000 // seen[s] - bps) <= 300, s


def test_mini_has_exactly_one_stale_open_opportunity(mini):
    plan, _, _, snapshots = mini
    snapshot = max(r["snapshot_date"] for r in snapshots)
    assert snapshot == "2026-03-31"
    stale = [r for r in snapshots if r["snapshot_date"] == snapshot and r["stage"] in STAGES
             and _d(r["expected_close_date"]) < _d(snapshot)]
    assert len(stale) == 1
    assert stale[0]["crm_account_id"] in plan.roles["stale_opportunity"]
    assert stale[0]["opp_type"] == "expansion"
    open_now = [r for r in snapshots if r["snapshot_date"] == snapshot and r["stage"] in STAGES]
    assert {r["opp_type"] for r in open_now} <= {"expansion", "renewal"}
    for r in snapshots:
        if r["stage"] in STAGES and r is not stale[0]:
            assert _d(r["expected_close_date"]) > _d(r["snapshot_date"])


def test_open_renewals_exist_for_customers_whose_term_ends_after_the_books(mini):
    plan, rows, _, snapshots = mini
    snapshot = max(r["snapshot_date"] for r in snapshots)
    open_renewals = [r for r in snapshots if r["snapshot_date"] == snapshot and r["opp_type"] == "renewal" and r["stage"] in STAGES]
    assert open_renewals
    contracted = {r["contract_id"] for r in rows}
    for r in open_renewals:
        assert "ctr_" + r["opp_id"].removeprefix("opp_") not in contracted


def test_without_lines_the_fee_comes_from_the_plan(cfg):
    plan = build_plan("mini", cfg, cfg["seed"])
    rows, _, _ = contracts(plan, rng)
    snapshots = pipeline(plan, rows, rng)
    customers = {c.crm_account_id: c for c in plan.customers}
    first = [r for r in snapshots if r["stage"] == "won" and r["opp_type"] == "new"]
    assert first and all(r["acv_cents"] == customers[r["crm_account_id"]].acv_cents for r in first)


def test_pipeline_is_deterministic(cfg):
    plan = build_plan("mini", cfg, cfg["seed"])
    rows, lines, _ = contracts(plan, rng)
    a = json.dumps(pipeline(plan, rows, rng, lines), sort_keys=True)
    assert a == json.dumps(pipeline(plan, list(reversed(rows)), rng, lines), sort_keys=True)
    other = build_plan("mini", cfg, cfg["seed"] + 1)
    orows, olines, _ = contracts(other, rng)
    assert json.dumps(pipeline(other, orows, rng, olines), sort_keys=True) != a
