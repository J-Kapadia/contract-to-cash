"""Monthly pipeline snapshots.

`pipeline(plan, contracts, rng, lines=None)` turns the contract rows of the books into the
rows of `pipeline_snapshots.csv`: for each month-end, every opportunity the CRM held that month.
The module is pure: the plan, the rows and the seeded streams (`rng(seed, entity, purpose)`, as
in `seeds.rng`) give the same rows every time.

    snapshot rows      snapshot_date, opp_id, crm_account_id, opp_type (`new` | `expansion` |
                       `renewal`), stage (`S1`..`S5` | `won` | `lost`), acv_cents, term_months,
                       billing_frequency, expected_close_date, known_on

Every first version of a contract is the outcome of an opportunity: it is `new` for the first
contract of a customer and `renewal` for each later one, it passes S1 to S5 in successive
month-ends, and it shows as `won` in the month-end of the month the contract was signed, with
the contract's term, billing cadence and (when `lines` is given) subscription fee. Its expected
close date is the date the contract was signed, so only its last open snapshot comes before it.
A stage never moves backwards and an opportunity that is won or lost is shown once, in the month
it closed. `opp_id` is `opp_<account number>_<contract number>` for a contract, so the contract
id names its opportunity.

Open at the last books month: the renewal of each customer whose term ends after the books and
who does not churn there, a share of customers' expansion opportunities, and, for the customer
the plan gives the `stale_opportunity` role, one expansion whose expected close date is already
past: the only open opportunity in the books to be stale. Lost opportunities are expansion
attempts on customers then in service, in numbers that make the share of opportunities seen in
each stage that go on to be won equal the policy default for that stage.

Choices made where the brief is silent: opportunities are tied to the plan's customers (there
are no prospects outside the CRM, so no open `new` opportunity); snapshots are not cut at the
first books month, so a contract signed just before it still shows its stages; the first two
stages last one or two month-ends and the others one; a stage never has an expected close date
in the past except for the stale opportunity; without `lines` the fee comes from the plan (the
customer's value with the expansions up to the contract's start, amendments not included); an
open renewal expected to close before the last month-end is expected on the next day; an open
expansion is a quarter of customers, worth 5 to 30 percent of their value, expected 20 to 120
days after the last month-end.
"""

from __future__ import annotations

import calendar
from collections import defaultdict
from collections.abc import Callable
from datetime import date, timedelta
from random import Random

from .plan import Customer, Plan
from .seeds import rng as default_rng

Rng = Callable[[int, str, str], Random]

PIPELINE_COLUMNS = (
    "snapshot_date", "opp_id", "crm_account_id", "opp_type", "stage", "acv_cents", "term_months",
    "billing_frequency", "expected_close_date", "known_on",
)
STAGES = ("S1", "S2", "S3", "S4", "S5")
STAGE_WIN_BPS = (1000, 2000, 4000, 6000, 8000)
STAGE_MONTHS = ((1, 2), (1, 2), (1, 1), (1, 1), (1, 1))
RENEWAL_CLOSE_LEAD_DAYS = 26
OPEN_EXPANSION_BPS = 2500
EXPANSION_ACV_BPS = (500, 3000)
OPEN_CLOSE_DAYS = (20, 120)
STALE_DAYS = (5, 25)
LOST_CLOSE_DAYS = (1, 60)


def _idx(month: str) -> int:
    year, mon = month.split("-")
    return int(year) * 12 + int(mon) - 1


def _last_of(idx: int) -> date:
    year, mon = idx // 12, idx % 12 + 1
    return date(year, mon, calendar.monthrange(year, mon)[1])


def _date_idx(d: date) -> int:
    return d.year * 12 + d.month - 1


def _months_between(start: date, end: date) -> int:
    """Whole months from the first day of `start`'s month through the last day of `end`'s."""
    return _date_idx(end) - _date_idx(start) + 1


def _live(c: Customer, idx: int) -> bool:
    return _idx(c.start_month) <= idx and (not c.churn_month or idx < _idx(c.churn_month))


def _durations(rng: Rng, seed: int, opp_id: str) -> list[int]:
    r = rng(seed, opp_id, "pipeline:stages")
    return [r.randint(*span) for span in STAGE_MONTHS]


def _path(durations: list[int], first: int, stages: int = len(STAGES)) -> list[tuple[int, str]]:
    """(month index, stage) for each month-end from `first`, `stages` stages long."""
    out: list[tuple[int, str]] = []
    at = first
    for stage, length in zip(STAGES[:stages], durations):
        for _ in range(length):
            out.append((at, stage))
            at += 1
    return out


def _path_at(durations: list[int], last: int, stage: int, offset: int) -> list[tuple[int, str]]:
    """The path of an opportunity that is `offset` month-ends into 1-based `stage` at `last`."""
    return _path(durations, last - sum(durations[: stage - 1]) - offset)


def _lost_counts(won: int) -> list[int]:
    """Lost opportunities whose last open stage is S1..S5, for `won` won ones: the share of
    those seen in stage s that are won is `STAGE_WIN_BPS[s]`."""
    reaching = [won * (10_000 - bps) // bps for bps in STAGE_WIN_BPS]
    return [reaching[i] - (reaching[i + 1] if i + 1 < len(reaching) else 0) for i in range(len(reaching))]


def _fees(lines: list[dict] | None) -> dict[tuple[str, int], int]:
    return {(ln["contract_id"], ln["version"]): ln["annual_fee_cents"]
            for ln in lines or [] if ln["product"] == "subscription"}


def _plan_fee(c: Customer, start: date) -> int:
    fee = c.acv_cents
    for e in c.expansion_plan:
        if _idx(e.month) <= _date_idx(start):
            fee = fee * (10_000 + e.uplift_bps) // 10_000
    return fee


def _opp_id(acct: str, suffix: str) -> str:
    return f"opp_{acct.removeprefix('acct_')}_{suffix}"


def _row(snapshot: date, opp: dict, stage: str) -> dict:
    return {
        "snapshot_date": snapshot.isoformat(), "opp_id": opp["opp_id"], "crm_account_id": opp["acct"],
        "opp_type": opp["type"], "stage": stage, "acv_cents": opp["acv"], "term_months": opp["term"],
        "billing_frequency": opp["frequency"], "expected_close_date": opp["close"].isoformat(),
        "known_on": snapshot.isoformat(),
    }


def pipeline(plan: Plan, contracts: list[dict], rng: Rng = default_rng,
             lines: list[dict] | None = None) -> list[dict]:
    """The pipeline snapshot rows for the contract rows of `plan`'s books."""
    seed = plan.seed
    first, last = _idx(plan.first_month), _idx(plan.last_month)
    last_end = _last_of(last)
    customers = {c.crm_account_id: c for c in plan.customers}
    fees = _fees(lines)
    firsts = sorted((r for r in contracts if r["version"] == 1),
                    key=lambda r: (r["crm_account_id"], r["renewal_number"]))

    # (opportunity, month-ends it is open in, month it closes in or None, closing stage)
    opps: list[tuple[dict, list[tuple[int, str]], int | None, str]] = []
    latest: dict[str, dict] = {}
    for r in firsts:
        c = customers[r["crm_account_id"]]
        start, end = date.fromisoformat(r["term_start"]), date.fromisoformat(r["term_end"])
        signed = date.fromisoformat(r["signed_on"])
        opp_id = _opp_id(c.crm_account_id, f"{r['renewal_number']:02d}")
        opp = {"opp_id": opp_id, "acct": c.crm_account_id, "type": "new" if r["renewal_number"] == 0 else "renewal",
               "acv": fees.get((r["contract_id"], 1), _plan_fee(c, start)),
               "term": _months_between(start, end), "frequency": r["billing_frequency"], "close": signed}
        durations = _durations(rng, seed, opp_id)
        closed = _date_idx(signed)
        opps.append((opp, _path(durations, closed - sum(durations)), closed, "won"))
        latest[c.crm_account_id] = r

    # The next renewal of each customer whose last contract ends after the books.
    for acct, r in sorted(latest.items()):
        c = customers[acct]
        nxt = date.fromisoformat(r["term_end"]) + timedelta(days=1)
        if _date_idx(nxt) <= last or _date_idx(nxt) > last + 12 or c.churn_month == f"{nxt.year:04d}-{nxt.month:02d}":
            continue
        opp_id = _opp_id(acct, f"{r['renewal_number'] + 1:02d}")
        close = max(nxt - timedelta(days=RENEWAL_CLOSE_LEAD_DAYS), last_end + timedelta(days=1))
        opp = {"opp_id": opp_id, "acct": acct, "type": "renewal",
               "acv": _plan_fee(c, nxt), "term": c.term_months, "frequency": c.billing_frequency, "close": close}
        durations = _durations(rng, seed, opp_id)
        opps.append((opp, [(i, s) for i, s in _path(durations, _date_idx(close) - sum(durations)) if i <= last],
                     None, ""))

    # Expansion attempts: open ones at the last month (one stale), and lost ones along the way.
    extras: defaultdict[str, int] = defaultdict(int)

    def expansion(c: Customer, close: date, name: str) -> dict:
        extras[c.crm_account_id] += 1
        r = rng(seed, c.crm_account_id, f"pipeline:{name}:{extras[c.crm_account_id]}")
        return {"opp_id": _opp_id(c.crm_account_id, f"x{extras[c.crm_account_id]:02d}"),
                "acct": c.crm_account_id, "type": "expansion",
                "acv": c.acv_cents * r.randint(*EXPANSION_ACV_BPS) // 10_000,
                "term": c.term_months, "frequency": c.billing_frequency, "close": close}

    stale = set(plan.roles.get("stale_opportunity", ()))
    for c in plan.customers:
        if not _live(c, last):
            continue
        r = rng(seed, c.crm_account_id, "pipeline:open")
        if c.crm_account_id in stale:
            close = last_end - timedelta(days=r.randint(*STALE_DAYS))
            stage = r.randint(2, 4)
        elif r.randrange(10_000) < OPEN_EXPANSION_BPS:
            close = last_end + timedelta(days=r.randint(*OPEN_CLOSE_DAYS))
            stage = r.randint(1, len(STAGES))
        else:
            continue
        opp = expansion(c, close, "open")
        durations = _durations(rng, seed, opp["opp_id"])
        offset = r.randrange(durations[stage - 1])
        opps.append((opp, _path_at(durations, last, stage, offset), None, ""))

    won = len(firsts)
    live_in = {m: [c for c in plan.customers if _live(c, m)] for m in range(first, last + 1)}
    months = [m for m, live in live_in.items() if live]
    n = 0
    for reached, count in enumerate(_lost_counts(won), start=1):
        for _ in range(count):
            n += 1
            r = rng(seed, "pipeline", f"lost:{n}")
            lost_at = months[r.randrange(len(months))]
            c = live_in[lost_at][r.randrange(len(live_in[lost_at]))]
            opp = expansion(c, date(1970, 1, 1), "lost")
            durations = _durations(rng, seed, opp["opp_id"])
            path = _path(durations, lost_at - sum(durations[:reached]), reached)
            opp["close"] = _last_of(lost_at - 1) + timedelta(days=r.randint(*LOST_CLOSE_DAYS))
            opps.append((opp, path, lost_at, "lost"))

    out = []
    for opp, path, closed_at, closing in opps:
        for idx, stage in path:
            if idx <= last:
                out.append(_row(_last_of(idx), opp, stage))
        if closed_at is not None and closed_at <= last:
            out.append(_row(_last_of(closed_at), opp, closing))
    out.sort(key=lambda r: (r["snapshot_date"], r["opp_id"]))
    return out
