"""Invoices, monthly usage and overage from contract rows.

`billing(plan, contracts, lines, rng)` turns contract and line rows (dicts keyed by the
`contracts.csv` and `contract_lines.csv` columns) into the `invoices.csv` and
`usage_monthly.csv` rows, as `(invoice_rows, usage_rows)`. The module is pure: the plan, the
rows and the seeded streams (`rng(seed, entity, purpose)`, as in `seeds.rng`) give the same
rows every time. Every amount is an integer of cents and every row carries `known_on`.

Subscription lines are invoiced in advance from `term_start`, one invoice per billing period
(`annual` = 12 months, `quarterly` = 3) up to `term_end`, dated the first day of the period
and due `payment_terms_days` later. A quarterly period is a quarter of the annual fee, the
four quarters of a year summing to the fee exactly (`truth_reference.split_exact`), so an
unamended contract invoices its annual fee once per contract year. `gl_period` is the month
of the invoice date.

A contract version after the first is an amendment: the latest version effective on a date
governs the periods starting on or after it. When it takes effect inside a period, an
invoice dated its effective date carries one stub per changed line, for the days from the
effective date to the end of that period (the contract's remaining term when it has one
period left, so a one-year contract is coterminous). The stub is the change in the period's
price, less its elapsed part by the ratable definition (`truth_reference`): an increase or a new line is a
`subscription` line, a decrease or an ended line a negative `credit` line.

A contract with an `overage` line draws one `usage_monthly` row per month of service inside
the books; when the units exceed the governing line's `included_units`, an `overage`
invoice dated the last day of the usage month bills the excess at `overage_cents_per_unit`,
for that month's service period. Only the first overage line (by id) of the governing version
is used.

Choices made where the brief is silent: the billing period grid, and the payment terms of a
stub, follow the governing version at the date they are used (the grid is the first
version's `billing_frequency` and `term_start`); a period that does not fill a whole quarter
or year at the end of the term still bills the full period amount; usage is drawn between 70
and 130 percent of the included units (100 to 1000 units when none are included); a usage
row is known on the last day of its month; invoice ids are `inv_000001`.. in invoice-date
order; a customer's `billing_customer_id` is the one `variants` assigns; nothing dated after
the last month of the books is emitted.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable, Iterable
from datetime import date, timedelta
from random import Random

from c2c.truth_reference import _cumulative, month_end, month_of, month_start, next_month, period_days, split_exact

from .plan import Plan
from .seeds import rng as default_rng
from .variants import variants

Rng = Callable[[int, str, str], Random]

PERIOD_MONTHS = {"annual": 12, "quarterly": 3}
QUARTERS = {0: 2500, 1: 2500, 2: 2500, 3: 2500}
USAGE_LOW_BPS, USAGE_HIGH_BPS = 7000, 13000
USAGE_IF_NONE_INCLUDED = (100, 1000)

INVOICE_COLUMNS = (
    "invoice_id", "line_no", "billing_customer_id", "contract_id", "contract_line_id",
    "invoice_date", "gl_period", "due_date", "service_start", "service_end", "line_type",
    "amount_cents", "known_on",
)
USAGE_COLUMNS = ("contract_id", "month", "units", "known_on")


def _add_months(day: date, months: int) -> date:
    idx = day.year * 12 + day.month - 1 + months
    year, mon = divmod(idx, 12)
    return date(year, mon + 1, 1)


def _periods(first: dict) -> list[tuple[date, date]]:
    """The billing periods of a contract: inclusive (start, end) from `term_start` to `term_end`."""
    start, term_end = date.fromisoformat(first["term_start"]), date.fromisoformat(first["term_end"])
    step = PERIOD_MONTHS[first["billing_frequency"]]
    out, k = [], 0
    while True:
        ps = _add_months(start, k * step)
        if ps > term_end:
            return out
        out.append((ps, min(_add_months(start, (k + 1) * step) - timedelta(days=1), term_end)))
        k += 1


def _period_price(first: dict, annual_fee_cents: int, index: int) -> int:
    """What a line of this annual fee bills in billing period `index` of the contract."""
    if first["billing_frequency"] == "annual":
        return annual_fee_cents
    return split_exact(annual_fee_cents, QUARTERS)[index % 4]


def _stub_cents(change: int, period_start: date, period_end: date, effective: date) -> int:
    """The part of a period's price `change` still to run from `effective`: the change less
    what the ratable definition counts as elapsed before it, so the stub and the elapsed part
    sum to the change exactly."""
    return change - _cumulative(change, period_start, period_days(period_start, period_end), effective - timedelta(days=1))


def _governing(versions: list[dict], day: date) -> dict:
    """The latest version effective on `day` (the first version when none is)."""
    current = versions[0]
    for v in versions:
        if date.fromisoformat(v["effective_from"]) <= day:
            current = v
    return current


def _fees(lines_by_version: dict[int, list[dict]], version: dict, product: str) -> dict[str, dict]:
    return {ln["line_id"]: ln for ln in lines_by_version.get(int(version["version"]), []) if ln["product"] == product}


def billing(plan: Plan, contracts: Iterable[dict], lines: Iterable[dict], rng: Rng = default_rng) -> tuple[list[dict], list[dict]]:
    """The `invoices.csv` and `usage_monthly.csv` rows for the given contract versions and lines."""
    billing_ids = variants(plan, rng).billing_customer_ids
    last_day = month_end(plan.last_month)

    by_contract: dict[str, list[dict]] = defaultdict(list)
    for row in contracts:
        by_contract[row["contract_id"]].append(row)
    line_rows: dict[tuple[str, int], list[dict]] = defaultdict(list)
    for row in lines:
        line_rows[(row["contract_id"], int(row["version"]))].append(row)

    drafts: list[tuple[date, str, int, dict]] = []  # (invoice date, contract, order, fields + line fields)
    usage_rows: list[dict] = []

    for contract_id in sorted(by_contract):
        versions = sorted(by_contract[contract_id], key=lambda v: int(v["version"]))
        first = versions[0]
        if first["crm_account_id"] not in billing_ids:
            raise ValueError(f"{contract_id}: {first['crm_account_id']} is not a customer of the plan")
        customer = billing_ids[first["crm_account_id"]]
        by_version = {int(v["version"]): line_rows.get((contract_id, int(v["version"])), []) for v in versions}
        term_start, term_end = date.fromisoformat(first["term_start"]), date.fromisoformat(first["term_end"])
        periods = _periods(first)

        def emit(day, order, line, line_type, amount, svc_start, svc_end, version):
            if day > last_day or amount == 0:
                return
            drafts.append((day, contract_id, order, {
                "billing_customer_id": customer, "contract_id": contract_id,
                "contract_line_id": line["line_id"], "invoice_date": day.isoformat(),
                "gl_period": month_of(day), "due_date": (day + timedelta(days=int(version["payment_terms_days"]))).isoformat(),
                "service_start": svc_start.isoformat(), "service_end": svc_end.isoformat(),
                "line_type": line_type, "amount_cents": amount, "known_on": day.isoformat(),
            }))

        # Subscription periods, in advance, at the version governing the period start.
        for i, (ps, pe) in enumerate(periods):
            version = _governing(versions, ps)
            for _, line in sorted(_fees(by_version, version, "subscription").items()):
                emit(ps, 0, line, "subscription", _period_price(first, line["annual_fee_cents"], i), ps, pe, version)

        # Amendments effective inside a period: stubs and credits for the price change.
        for prev, version in zip(versions, versions[1:]):
            eff = date.fromisoformat(version["effective_from"])
            for i, (ps, pe) in enumerate(periods):
                if not ps < eff <= pe:
                    continue
                old, new = _fees(by_version, prev, "subscription"), _fees(by_version, version, "subscription")
                for line_id in sorted(set(old) | set(new)):
                    before = _period_price(first, old[line_id]["annual_fee_cents"], i) if line_id in old else 0
                    after = _period_price(first, new[line_id]["annual_fee_cents"], i) if line_id in new else 0
                    change = after - before
                    stub = _stub_cents(abs(change), ps, pe, eff)
                    line = new.get(line_id) or old[line_id]
                    emit(eff, 1, line, "subscription" if change > 0 else "credit", stub if change > 0 else -stub, eff, pe, version)

        # Usage and overage, one month at a time through the books.
        if any(ln["product"] == "overage" for rows in by_version.values() for ln in rows):
            month = month_of(term_start)
            while month <= min(month_of(term_end), plan.last_month):
                end_of_month = month_end(month)
                version = _governing(versions, end_of_month)
                over = _fees(by_version, version, "overage")
                if over:
                    line = over[sorted(over)[0]]
                    included = line["included_units"]
                    r = rng(plan.seed, contract_id, f"usage:{month}")
                    if included:
                        units = included * r.randint(USAGE_LOW_BPS, USAGE_HIGH_BPS) // 10_000
                    else:
                        units = r.randint(*USAGE_IF_NONE_INCLUDED)
                    usage_rows.append({"contract_id": contract_id, "month": month, "units": units,
                                       "known_on": end_of_month.isoformat()})
                    excess = max(units - included, 0)
                    emit(end_of_month, 2, line, "overage", excess * line["overage_cents_per_unit"],
                         month_start(month), end_of_month, version)
                month = next_month(month)

    drafts.sort(key=lambda d: (d[0], d[1], d[2], d[3]["contract_line_id"]))
    invoice_rows, numbered, line_no = [], {}, {}
    for day, contract_id, order, fields in drafts:
        key = (day, contract_id, order)
        if key not in numbered:
            numbered[key] = f"inv_{len(numbered) + 1:06d}"
        invoice_id = numbered[key]
        line_no[invoice_id] = line_no.get(invoice_id, 0) + 1
        row = {"invoice_id": invoice_id, "line_no": line_no[invoice_id], **fields}
        invoice_rows.append({c: row[c] for c in INVOICE_COLUMNS})

    usage_rows.sort(key=lambda u: (u["contract_id"], u["month"]))
    return invoice_rows, usage_rows
