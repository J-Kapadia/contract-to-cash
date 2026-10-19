"""An independent Python reference for the money arithmetic.

The warehouse computes ratable revenue, roll-forwards, the revenue-to-cash bridge and ARR
in SQL. This module computes the same numbers a second way, in plain integers, so that
"the warehouse equals the answer key" compares two implementations. It is used by the
generator's truth writer and by tests; the close, forecast and memo steps never import it.

Everything is exact: amounts are `int` cents, rates are `int` basis points, dates are
`datetime.date` (ISO strings are accepted in rows), months are `"YYYY-MM"` strings. There
is no pandas and no float anywhere.

Row shapes follow the books files: invoice rows carry `invoice_id`, `billing_customer_id`,
`invoice_date`, `service_start`, `service_end`, `line_type` and a signed `amount_cents`;
payment rows carry `billing_customer_id`, `applied_cents` and `applied_on`; bank rows carry
`posted_on` and a signed `amount_cents`, the first row being the opening balance.
"""

from __future__ import annotations

import calendar
from collections import defaultdict
from collections.abc import Hashable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

Row = Mapping[str, Any]

BRIDGE_LINES = ("revenue", "defrev_movement", "net_billings", "ar_movement", "receipts")


# -- dates and months ---------------------------------------------------------------------------

def _date(value: date | str) -> date:
    return value if isinstance(value, date) else date.fromisoformat(value)


def month_of(day: date | str) -> str:
    day = _date(day)
    return f"{day.year:04d}-{day.month:02d}"


def month_start(month: str) -> date:
    year, mon = month.split("-")
    return date(int(year), int(mon), 1)


def month_end(month: str) -> date:
    first = month_start(month)
    return first.replace(day=calendar.monthrange(first.year, first.month)[1])


def next_month(month: str) -> str:
    return month_of(month_end(month) + timedelta(days=1))


def months_between(first: str, last: str) -> list[str]:
    """Every month from `first` through `last` inclusive (empty when `last` precedes `first`)."""
    out = []
    while first <= last:
        out.append(first)
        first = next_month(first)
    return out


# -- ratable revenue and exact splits -----------------------------------------------------------

def period_days(service_start: date | str, service_end: date | str) -> int:
    """Days in the inclusive service period."""
    days = (_date(service_end) - _date(service_start)).days + 1
    if days < 1:
        raise ValueError(f"service period ends before it starts: {service_start} .. {service_end}")
    return days


def _cumulative(amount: int, start: date, days: int, through: date) -> int:
    """The amount recognised through the end of `through`: amount x elapsed days // total days."""
    elapsed = min(max((through - start).days + 1, 0), days)
    return amount * elapsed // days


def ratable_cents(amount: int, service_start: date | str, service_end: date | str, month: str) -> int:
    """The part of `amount` recognised in `month`, by day over the inclusive service period.

    The cumulative amount through day d of n is `amount * d // n`; a month's share is the
    cumulative at its last day less the cumulative at the day before its first, so the months
    of a period sum to `amount` exactly, whatever the grouping.
    """
    start = _date(service_start)
    days = period_days(start, service_end)
    return (_cumulative(amount, start, days, month_end(month))
            - _cumulative(amount, start, days, month_start(month) - timedelta(days=1)))


def weighted_cents(amount: int, weight_bps: int) -> int:
    return amount * weight_bps // 10_000


def split_exact(total: int, weights_bps: Mapping[Hashable, int]) -> dict[Hashable, int]:
    """Split `total` in proportion to the weights so that the parts sum to `total` exactly.

    Each part starts at `total * w // sum(w)`; the cents left over go one each to the keys with
    the largest remainders, ties by ascending key. Weights need not sum to 10000.
    """
    if not weights_bps:
        raise ValueError("split_exact needs at least one weight")
    if any(w < 0 for w in weights_bps.values()):
        raise ValueError("split_exact weights must not be negative")
    weight_sum = sum(weights_bps.values())
    if weight_sum == 0:
        raise ValueError("split_exact weights sum to zero")
    parts = {k: total * w // weight_sum for k, w in weights_bps.items()}
    remainders = {k: total * w % weight_sum for k, w in weights_bps.items()}
    leftover = total - sum(parts.values())
    for key in sorted(parts, key=lambda k: (-remainders[k], k))[:leftover]:
        parts[key] += 1
    return parts


# -- order statistics and rates -----------------------------------------------------------------

def lower_median(xs: Iterable[int]) -> int:
    """The value at 0-based index (n - 1) // 2 of the sorted data; always a value in the data."""
    ordered = sorted(xs)
    if not ordered:
        raise ValueError("lower_median of no values")
    return ordered[(len(ordered) - 1) // 2]


def percentile_nearest_rank(xs: Iterable[int], p: int) -> int:
    """The value at 1-based rank ceil(p * n / 100) of the sorted data (rank at least 1)."""
    ordered = sorted(xs)
    if not ordered:
        raise ValueError("percentile of no values")
    if not 0 <= p <= 100:
        raise ValueError(f"percentile must be between 0 and 100, got {p}")
    rank = max(-(-p * len(ordered) // 100), 1)
    return ordered[rank - 1]


def rate_bps(num: int, den: int) -> int:
    """`num / den` in whole basis points, rounded down."""
    if den <= 0:
        raise ValueError("rate_bps needs a positive denominator")
    return num * 10_000 // den


# -- roll-forwards ------------------------------------------------------------------------------

@dataclass(frozen=True)
class Roll:
    """One month of a roll-forward: opening + additions - reductions = closing."""
    opening: int
    additions: int
    reductions: int
    closing: int


@dataclass
class RollForwards:
    """Per customer per month: revenue by line type, deferred revenue and AR roll-forwards."""
    months: list[str] = field(default_factory=list)
    revenue: dict[tuple[str, str, str], int] = field(default_factory=dict)  # (customer, month, line_type)
    defrev: dict[tuple[str, str], Roll] = field(default_factory=dict)       # (customer, month)
    ar: dict[tuple[str, str], Roll] = field(default_factory=dict)           # (customer, month)

    def revenue_total(self, month: str) -> int:
        return sum(v for (_, m, _), v in self.revenue.items() if m == month)


def _roll(opening: int, additions: int, reductions: int) -> Roll:
    return Roll(opening, additions, reductions, opening + additions - reductions)


def rollforward(invoices: Iterable[Row], payments: Iterable[Row], through: str | None = None,
                customer_key: str = "billing_customer_id") -> RollForwards:
    """Revenue, deferred revenue and AR per customer per month, from the first month with a
    row (the books open at zero) through `through` (default: the last month with a row or a
    service day).

    - Subscription and credit lines are recognised by day over their service period; an
      overage line is recognised in the month its service starts (the usage month).
    - Deferred revenue: opening + subscription and credit billings - recognised revenue of
      those lines. Overage is billed and recognised in the same month, so it never sits there.
    - AR: opening + billings (every non-credit line) - cash applied - credits (as a positive
      amount). Billings land in the invoice month, cash in the month applied.
    """
    revenue: dict[tuple[str, str, str], int] = defaultdict(int)
    defrev_billed: dict[tuple[str, str], int] = defaultdict(int)
    defrev_recognised: dict[tuple[str, str], int] = defaultdict(int)
    ar_billed: dict[tuple[str, str], int] = defaultdict(int)
    ar_collected: dict[tuple[str, str], int] = defaultdict(int)
    ar_credits: dict[tuple[str, str], int] = defaultdict(int)
    customers: set[str] = set()
    seen_months: list[str] = []

    for row in invoices:
        cust, amount, kind = str(row[customer_key]), int(row["amount_cents"]), row["line_type"]
        billed_in = month_of(row["invoice_date"])
        start, end = _date(row["service_start"]), _date(row["service_end"])
        customers.add(cust)
        seen_months += [billed_in, month_of(start), month_of(end)]
        if kind == "overage":
            ar_billed[cust, billed_in] += amount
            revenue[cust, month_of(start), kind] += amount
            continue
        if kind not in ("subscription", "credit"):
            raise ValueError(f"unknown invoice line_type {kind!r}")
        defrev_billed[cust, billed_in] += amount
        if kind == "credit":
            ar_credits[cust, billed_in] += -amount
        else:
            ar_billed[cust, billed_in] += amount
        for month in months_between(month_of(start), month_of(end)):
            share = ratable_cents(amount, start, end, month)
            revenue[cust, month, kind] += share
            defrev_recognised[cust, month] += share

    for row in payments:
        cust, applied_in = str(row[customer_key]), month_of(row["applied_on"])
        customers.add(cust)
        seen_months.append(applied_in)
        ar_collected[cust, applied_in] += int(row["applied_cents"])

    result = RollForwards()
    if not seen_months:
        return result
    last = through or max(seen_months)
    result.months = months_between(min(seen_months), last)
    result.revenue = {k: v for k, v in revenue.items() if k[1] in set(result.months)}
    for cust in sorted(customers):
        defrev_close = ar_close = 0
        for month in result.months:
            key = (cust, month)
            roll = _roll(defrev_close, defrev_billed[key], defrev_recognised[key])
            result.defrev[key], defrev_close = roll, roll.closing
            roll = _roll(ar_close, ar_billed[key], ar_collected[key] + ar_credits[key])
            result.ar[key], ar_close = roll, roll.closing
    return result


def cash_rollforward(bank_rows: Iterable[Row]) -> dict[str, Roll]:
    """Cash per month: opening + receipts - disbursements = closing.

    The first row is the opening balance, not a receipt. Positive amounts are receipts and
    negative ones disbursements (reported as positive amounts); months with no rows between
    the first and last are carried at their opening balance.
    """
    rows = list(bank_rows)
    if not rows:
        return {}
    balance = int(rows[0]["amount_cents"])
    receipts: dict[str, int] = defaultdict(int)
    disbursements: dict[str, int] = defaultdict(int)
    for row in rows[1:]:
        amount, month = int(row["amount_cents"]), month_of(row["posted_on"])
        if amount >= 0:
            receipts[month] += amount
        else:
            disbursements[month] += -amount
    seen = [month_of(r["posted_on"]) for r in rows]
    out: dict[str, Roll] = {}
    for month in months_between(min(seen), max(seen)):
        roll = _roll(balance, receipts[month], disbursements[month])
        out[month], balance = roll, roll.closing
    return out


# -- the revenue-to-cash bridge -----------------------------------------------------------------

def net_billings(invoices: Iterable[Row], month: str) -> int:
    """The signed sum of the month's invoice lines (a credit line is a negative billing)."""
    return sum(int(r["amount_cents"]) for r in invoices if month_of(r["invoice_date"]) == month)


def bridge(month: str, rolls: RollForwards) -> dict[str, int]:
    """The month's walk from recognised revenue to cash receipts.

    revenue + defrev_movement = net_billings, and net_billings - ar_movement = receipts,
    where a movement is closing less opening summed over customers. There is no plug line.
    """
    revenue = rolls.revenue_total(month)
    defrev_movement = sum(r.closing - r.opening for (_, m), r in rolls.defrev.items() if m == month)
    ar_movement = sum(r.closing - r.opening for (_, m), r in rolls.ar.items() if m == month)
    billings = revenue + defrev_movement
    return {"revenue": revenue, "defrev_movement": defrev_movement, "net_billings": billings,
            "ar_movement": ar_movement, "receipts": billings - ar_movement}


# -- ARR ----------------------------------------------------------------------------------------

def arr(contracts: Iterable[Row], on: date | str, customer_key: str = "crm_account_id") -> dict[str, int]:
    """ARR per customer on a date: the annual fees of subscription lines in service.

    Rows are contract lines joined to their version: `contract_id`, `version`, `effective_from`,
    `term_start`, `term_end`, `product`, `annual_fee_cents` and the customer key. A version
    lists every line in force from its `effective_from`; the latest version effective on the
    date governs, and a contract counts only between its term start and end. Customers with
    no ARR on the date are left out.
    """
    day = _date(on)
    versions: dict[str, dict[tuple[date, int], list[Row]]] = defaultdict(lambda: defaultdict(list))
    for row in contracts:
        effective = _date(row["effective_from"])
        if effective <= day:
            versions[str(row["contract_id"])][effective, int(row["version"])].append(row)
    out: dict[str, int] = defaultdict(int)
    for by_version in versions.values():
        lines = by_version[max(by_version)]
        for line in lines:
            if (_date(line["term_start"]) <= day <= _date(line["term_end"])
                    and line["product"] == "subscription"):
                out[str(line[customer_key])] += int(line["annual_fee_cents"])
    return {k: v for k, v in sorted(out.items()) if v}
