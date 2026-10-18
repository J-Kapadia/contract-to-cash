"""Contracts, amendments and renewals.

`contracts(plan, rng)` turns the entity plan into the contract rows of the books, in the
shape of `contracts.csv` and `contract_lines.csv`, and the renewal outcomes of the answer key
(`truth/renewal_outcomes.csv`). The module is pure: the plan and the seeded streams
(`rng(seed, entity, purpose)`, as in `seeds.rng`) give the same rows every time.

    contract rows      contract_id, version, crm_account_id, signed_on, effective_from,
                       term_start, term_end, billing_frequency, payment_terms_days,
                       renewal_number
    line rows          contract_id, version, line_id, product (`subscription` | `overage`),
                       annual_fee_cents, included_units, overage_cents_per_unit
    renewal outcomes   contract_id (the expiring contract), expiry (its term end), outcome
                       (`renewed` | `churned` | `expanded`), acv_before, acv_after

A contract is the signed agreement and carries its renewal number; an amendment is a further
version of the same contract, effective from a date inside the term and running to the term
end. Every version lists all of its lines, so the latest version effective on a date is the
whole governing state for that date. The date a row becomes known is its `signed_on`.

The plan decides who churns, who expands and by how much, and who is amended and in which
month; this module turns those decisions into dates and amounts. Renewals therefore cluster
in Q4 and Q1 because first signings do: a term ends a whole number of terms after it began.

Choices made where the brief is silent: contract rows cover the books window only (a contract
whose term starts after the last books month is not emitted), while renewal outcomes cover
every expiry up to the end of the truth horizon; a renewal keeps the term length, billing
cadence and payment terms and starts on the first day after the prior term; a renewal is
signed 7 to 45 days before it starts; an amendment is signed on its effective date, which is
a day of the plan's amendment month; it moves the annual fee by 500 to 3000 bps, up or down
with equal chance, and the renewal that follows starts from the amended fee; the overage line
includes one unit per 10000 cents of annual fee and prices a unit above that at 12500 cents.
"""

from __future__ import annotations

import calendar
from collections.abc import Callable
from datetime import date, timedelta
from random import Random

from .plan import Customer, Plan
from .seeds import rng as default_rng

Rng = Callable[[int, str, str], Random]

AMENDMENT_BPS = (500, 3000)
RENEWAL_LEAD_DAYS = (7, 45)
CENTS_PER_INCLUDED_UNIT = 10_000
OVERAGE_CENTS_PER_UNIT = 12_500


def _idx(month: str) -> int:
    year, mon = month.split("-")
    return int(year) * 12 + int(mon) - 1


def _first_of(idx: int) -> date:
    return date(idx // 12, idx % 12 + 1, 1)


def _last_of(idx: int) -> date:
    year, mon = idx // 12, idx % 12 + 1
    return date(year, mon, calendar.monthrange(year, mon)[1])


def _month_of(idx: int) -> str:
    return f"{idx // 12:04d}-{idx % 12 + 1:02d}"


def _contract_id(acct: str, renewal_number: int) -> str:
    return f"ctr_{acct.removeprefix('acct_')}_{renewal_number:02d}"


def _lines(contract_id: str, version: int, fee_cents: int) -> list[dict]:
    return [
        {"contract_id": contract_id, "version": version, "line_id": f"{contract_id}_L1",
         "product": "subscription", "annual_fee_cents": fee_cents,
         "included_units": 0, "overage_cents_per_unit": 0},
        {"contract_id": contract_id, "version": version, "line_id": f"{contract_id}_L2",
         "product": "overage", "annual_fee_cents": 0,
         "included_units": fee_cents // CENTS_PER_INCLUDED_UNIT,
         "overage_cents_per_unit": OVERAGE_CENTS_PER_UNIT},
    ]


def _row(c: Customer, contract_id: str, version: int, signed_on: date, effective_from: date,
         term_start: date, term_end: date, renewal_number: int) -> dict:
    return {
        "contract_id": contract_id, "version": version, "crm_account_id": c.crm_account_id,
        "signed_on": signed_on.isoformat(), "effective_from": effective_from.isoformat(),
        "term_start": term_start.isoformat(), "term_end": term_end.isoformat(),
        "billing_frequency": c.billing_frequency, "payment_terms_days": c.payment_terms_days,
        "renewal_number": renewal_number,
    }


def _amendment(c: Customer, rng: Rng, seed: int, term_start: date, term_end: date, fee: int
               ) -> tuple[date, int]:
    """The effective date and the new annual fee of the plan's mid-term amendment."""
    month = _idx(c.amendment_month)
    r = rng(seed, c.crm_account_id, "contracts:amendment")
    effective = _first_of(month) + timedelta(days=r.randint(0, 27))
    bps = r.randint(*AMENDMENT_BPS)
    new_fee = fee * (10_000 + (bps if r.random() < 0.5 else -bps)) // 10_000
    if not term_start < effective < term_end:
        raise ValueError(f"{c.crm_account_id}: amendment on {effective} is not inside the term "
                         f"{term_start} to {term_end}")
    return effective, new_fee


def amendment_kind(rows: list[dict], lines: list[dict], contract_id: str, version: int) -> str:
    """`upgrade` or `downgrade`: the subscription fee of `version` against the version before."""
    if version < 2:
        raise ValueError(f"{contract_id} version {version} is not an amendment")
    fee = {(ln["contract_id"], ln["version"]): ln["annual_fee_cents"]
           for ln in lines if ln["product"] == "subscription"}
    before, after = fee[(contract_id, version - 1)], fee[(contract_id, version)]
    if after == before:
        raise ValueError(f"{contract_id} version {version} does not change the fee")
    return "upgrade" if after > before else "downgrade"


def contracts(plan: Plan, rng: Rng = default_rng) -> tuple[list[dict], list[dict], list[dict]]:
    """The contract rows, line rows and renewal outcomes for every customer of `plan`."""
    last = _idx(plan.last_month)
    horizon_last = _idx(plan.truth_last_month or plan.last_month)
    rows: list[dict] = []
    lines: list[dict] = []
    outcomes: list[dict] = []

    for c in plan.customers:
        acct = c.crm_account_id
        expansions = {e.month: e.uplift_bps for e in c.expansion_plan}
        fee = c.acv_cents
        start = _idx(c.start_month)
        number = 0
        signed_on = _first_of(_idx(c.signing_month)) + timedelta(
            days=rng(plan.seed, acct, "contracts:signed:0").randint(0, 27))

        while True:
            end = start + c.term_months
            contract_id = _contract_id(acct, number)
            term_start, term_end = _first_of(start), _last_of(end - 1)
            rows.append(_row(c, contract_id, 1, signed_on, term_start, term_start, term_end, number))
            lines.extend(_lines(contract_id, 1, fee))
            if number == 0 and c.amendment_month:
                effective, fee = _amendment(c, rng, plan.seed, term_start, term_end, fee)
                rows.append(_row(c, contract_id, 2, effective, effective, term_start, term_end, number))
                lines.extend(_lines(contract_id, 2, fee))

            if end > horizon_last:
                break
            month = _month_of(end)
            if c.churn_month == month:
                outcomes.append({"contract_id": contract_id, "expiry": term_end.isoformat(),
                                 "outcome": "churned", "acv_before": fee, "acv_after": 0})
                break
            new_fee = fee * (10_000 + expansions.get(month, 0)) // 10_000
            outcomes.append({"contract_id": contract_id, "expiry": term_end.isoformat(),
                             "outcome": "expanded" if month in expansions else "renewed",
                             "acv_before": fee, "acv_after": new_fee})
            if end > last:
                break
            number += 1
            start, fee = end, new_fee
            lead = rng(plan.seed, acct, f"contracts:signed:{number}").randint(*RENEWAL_LEAD_DAYS)
            signed_on = _first_of(start) - timedelta(days=lead)

    rows.sort(key=lambda r: (r["contract_id"], r["version"]))
    lines.sort(key=lambda r: (r["contract_id"], r["version"], r["line_id"]))
    outcomes.sort(key=lambda r: (r["contract_id"], r["expiry"]))
    return rows, lines, outcomes
