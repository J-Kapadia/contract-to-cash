"""The expected collections file of a set of books: aged receivables, days to pay and expected cash.

`collections_<month>.csv` holds, for the last month of the books, one row per customer and aging
bucket, one row per customer with a days-to-pay statistic (the pooled row has customer `*`), and one
row per open invoice and case with its expected cash date. The arithmetic is plain integers and
dates, worked out from the books and the policy alone, never read from the warehouse.

Rows are the books' own: invoice rows carry `invoice_id`, `customer_id`, `invoice_date`, `due_date`
and a signed `amount_cents` (one row per line); application rows carry `invoice_id`, `customer_id`,
`applied_cents` and `applied_on`. A calendar is a sorted list of business days.

Where the books are silent: cash applied to an invoice the books do not hold has no due date and is
carried in `current`; a receipt with no payment row applies nothing (the `mini` books have none).
"""

from __future__ import annotations

import bisect
import csv
import io
import json
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import yaml

from c2c import truth_reference as tr

Row = Mapping[str, Any]

POOLED = "*"
CASES = ("base", "downside", "upside")
HORIZON_DAYS = 91
COLUMNS = (
    "table", "customer_id", "invoice_id", "bucket", "case", "open_cents",
    "paid_invoices", "median_days", "p80_days", "fallback", "expected_date", "cents", "at_risk",
)


def _date(value: date | str) -> date:
    return value if isinstance(value, date) else date.fromisoformat(value)


def _read(path: Path) -> list[dict]:
    with path.open(encoding="utf-8", newline="") as fh:
        return list(csv.DictReader(fh))


# -- the books, one row per invoice --------------------------------------------------------------

def _invoice_totals(invoices: Iterable[Row]) -> dict[str, dict]:
    """`invoice_id -> customer, invoice date, due date (the earliest of its lines), net total`."""
    out: dict[str, dict] = {}
    for row in invoices:
        found = out.setdefault(row["invoice_id"], {
            "customer_id": row["customer_id"], "invoice_date": _date(row["invoice_date"]),
            "due_date": _date(row["due_date"]), "cents": 0})
        found["invoice_date"] = min(found["invoice_date"], _date(row["invoice_date"]))
        found["due_date"] = min(found["due_date"], _date(row["due_date"]))
        found["cents"] += int(row["amount_cents"])
    return out


def _applied_by(applications: Iterable[Row], as_of: date) -> dict[str, int]:
    out: dict[str, int] = defaultdict(int)
    for row in applications:
        if row["invoice_id"] and _date(row["applied_on"]) <= as_of:
            out[row["invoice_id"]] += int(row["applied_cents"])
    return out


def paid_invoices(invoices: Iterable[Row], applications: Iterable[Row]) -> dict[str, dict]:
    """The invoices the payments applied to them cover, by the date they became fully covered.

    The paid date is the application date on which the running total first reaches the invoice's net
    total, so an invoice paid in instalments takes the date of the last one it needed.
    """
    totals = _invoice_totals(invoices)
    by_invoice: dict[str, list[tuple[date, int]]] = defaultdict(list)
    for row in applications:
        if row["invoice_id"] in totals:
            by_invoice[row["invoice_id"]].append((_date(row["applied_on"]), int(row["applied_cents"])))
    paid = {}
    for invoice_id, parts in by_invoice.items():
        running = 0
        for on, cents in sorted(parts):
            running += cents
            if running >= totals[invoice_id]["cents"]:
                paid[invoice_id] = {**totals[invoice_id], "paid_on": on}
                break
    return paid


# -- aged receivables ----------------------------------------------------------------------------

def bucket_names(buckets: Sequence[int]) -> list[str]:
    """`current`, then one `lo-hi` per pair of edges, then `hi+` past the last edge."""
    names = ["current"]
    names += [f"{lo + 1}-{hi}" for lo, hi in zip(buckets, buckets[1:])]
    return names + [f"{buckets[-1] + 1}+"]


def bucket_of(days_past_due: int, buckets: Sequence[int]) -> str:
    names = bucket_names(buckets)
    for edge, name in zip(buckets, names):
        if days_past_due <= edge:
            return name
    return names[-1]


def aging(invoices: Iterable[Row], applications: Iterable[Row], as_of: date | str,
          buckets: Sequence[int]) -> dict[tuple[str, str], int]:
    """Open cents per `(customer, bucket)` at `as_of`, every bucket of every customer present.

    An invoice dated on or before `as_of` owes its net total (credit lines included) less the
    payments applied by then, in the bucket of its days past due. Customers are those of the
    invoices and applications given.
    """
    day = _date(as_of)
    applications = list(applications)
    totals = _invoice_totals(invoices)
    applied = _applied_by(applications, day)
    out: dict[tuple[str, str], int] = {}
    customers = {t["customer_id"] for t in totals.values()} | {a["customer_id"] for a in applications}
    for customer in customers:
        for name in bucket_names(buckets):
            out[customer, name] = 0
    for invoice_id, t in totals.items():
        if t["invoice_date"] <= day:
            out[t["customer_id"], bucket_of((day - t["due_date"]).days, buckets)] += t["cents"] - applied.get(invoice_id, 0)
    for row in applications:
        if row["invoice_id"] not in totals and _date(row["applied_on"]) <= day:
            out[row["customer_id"], "current"] -= int(row["applied_cents"])
    return out


# -- days to pay ---------------------------------------------------------------------------------

def _months(day: date) -> int:
    return day.year * 12 + day.month


def _in_window(paid_on: date, as_of: date, window_months: int) -> bool:
    return _months(as_of) - window_months < _months(paid_on) <= _months(as_of)


def days_to_pay(invoices: Iterable[Row], applications: Iterable[Row], as_of: date | str,
                window_months: int, min_paid: int) -> dict[str, tuple[int, int, int, bool]]:
    """`customer -> (paid invoices, median days, p80 days, fallback)` at `as_of`, plus the pooled row.

    Days to pay is the paid date less the invoice date, over the invoices paid in the trailing
    `window_months` months (the month of `as_of` included): the lower median and the nearest-rank
    80th percentile. A customer with fewer than `min_paid` of them takes the pooled values and says
    so; a customer with none has no row. With no paid invoice in the window the result is empty.
    """
    day = _date(as_of)
    window = [p for p in paid_invoices(invoices, applications).values() if _in_window(p["paid_on"], day, window_months)]
    if not window:
        return {}
    pooled = [(p["paid_on"] - p["invoice_date"]).days for p in window]
    pool = (tr.lower_median(pooled), tr.percentile_nearest_rank(pooled, 80))
    out = {POOLED: (len(pooled), *pool, False)}
    for customer in sorted({p["customer_id"] for p in window}):
        own = [(p["paid_on"] - p["invoice_date"]).days for p in window if p["customer_id"] == customer]
        if len(own) < min_paid:
            out[customer] = (len(own), *pool, True)
        else:
            out[customer] = (len(own), tr.lower_median(own), tr.percentile_nearest_rank(own, 80), False)
    return out


def median_lateness(invoices: Iterable[Row], applications: Iterable[Row], as_of: date | str,
                    window_months: int, min_paid: int) -> dict[str, int]:
    """`customer -> median days paid after due` over the window, never below zero; a customer with
    fewer than `min_paid` paid invoices takes the pooled median. The pooled row is `*`."""
    day = _date(as_of)
    window = [p for p in paid_invoices(invoices, applications).values() if _in_window(p["paid_on"], day, window_months)]
    if not window:
        return {}
    pooled = max(tr.lower_median([(p["paid_on"] - p["due_date"]).days for p in window]), 0)
    out = {POOLED: pooled}
    for customer in {p["customer_id"] for p in window}:
        own = [(p["paid_on"] - p["due_date"]).days for p in window if p["customer_id"] == customer]
        out[customer] = max(tr.lower_median(own), 0) if len(own) >= min_paid else pooled
    return out


# -- expected cash dates -------------------------------------------------------------------------

def expected_pay_date(invoice: Row, as_of: date | str, dtp_days: int | None, lateness: int | None,
                      calendar: Sequence[date]) -> date:
    """The date an open invoice is expected to be paid, as of `as_of`.

    The invoice date plus the case's days to pay (the terms when there are none). A date not after
    `as_of` is past, so it becomes the day after `as_of` plus the customer's lateness, never later
    than 91 days after `as_of`. The result moves to the next business day on or after it; when that
    moves a shifted date past the 91st day, the last business day on or before that day is used.
    """
    day, issued, due = _date(as_of), _date(invoice["invoice_date"]), _date(invoice["due_date"])
    nominal = issued + timedelta(days=(due - issued).days if dtp_days is None else dtp_days)
    allowed = day + timedelta(days=HORIZON_DAYS)
    wanted = nominal if nominal > day else min(day + timedelta(days=1 + (lateness or 0)), allowed)
    at = bisect.bisect_left(calendar, wanted)
    moved = calendar[at] if at < len(calendar) else wanted
    if nominal > day or moved <= allowed:
        return moved
    before = bisect.bisect_right(calendar, allowed)
    return calendar[before - 1] if before else allowed


def expected_cash(invoices: Iterable[Row], applications: Iterable[Row], as_of: date | str, *,
                  window_months: int, min_paid: int, at_risk_days: int,
                  calendar: Sequence[date]) -> list[dict]:
    """One row per open invoice and case at `as_of`: customer, expected date, cents, at-risk flag.

    Base takes the customer's median days to pay, downside its 80th percentile and upside the
    lesser of the median and the invoice's terms; a customer with no row takes the pooled one. An
    invoice more than `at_risk_days` past due brings no cash in base or upside, and the downside
    keeps its open amount, flagged.
    """
    day = _date(as_of)
    invoices, applications = list(invoices), list(applications)
    dtp = days_to_pay(invoices, applications, day, window_months, min_paid)
    late = median_lateness(invoices, applications, day, window_months, min_paid)
    applied = _applied_by(applications, day)
    rows = []
    for invoice_id, t in sorted(_invoice_totals(invoices).items()):
        open_cents = t["cents"] - applied.get(invoice_id, 0)
        if t["invoice_date"] > day or open_cents <= 0:
            continue
        customer = t["customer_id"]
        _, median, p80, _ = dtp.get(customer) or dtp.get(POOLED) or (0, None, None, False)
        lateness = late.get(customer, late.get(POOLED))
        terms = (t["due_date"] - t["invoice_date"]).days
        risk = (day - t["due_date"]).days > at_risk_days
        by_case = {"base": median, "downside": p80, "upside": terms if median is None else min(median, terms)}
        for case in CASES:
            rows.append({
                "invoice_id": invoice_id, "customer_id": customer, "case": case,
                "expected_date": expected_pay_date(t, day, by_case[case], lateness, calendar),
                "cents": 0 if risk and case != "downside" else open_cents, "at_risk": risk,
            })
    return rows


# -- the expected file ---------------------------------------------------------------------------

def _flag(value: bool) -> str:
    return "true" if value else "false"


def rows_for(books: Path, truth: Path, config: Path, calendar: Sequence[date]) -> tuple[str, list[dict]]:
    """`(month, rows)`: the last month of the books and its rows, aging then days to pay then cash."""
    customer_of = {r["source_id"]: r["true_customer_id"]
                   for r in _read(truth / "identities.csv") if r["source_system"] == "billing"}
    window = json.loads((books / "window.json").read_text(encoding="utf-8"))
    policy = yaml.safe_load((config / "policy.yaml").read_text(encoding="utf-8"))
    invoices = [{**r, "customer_id": customer_of[r["billing_customer_id"]]} for r in _read(books / "invoices.csv")]
    customer_of_invoice = {r["invoice_id"]: r["customer_id"] for r in invoices}
    applications = [{**r, "customer_id": customer_of_invoice.get(r["invoice_id"], customer_of[r["billing_customer_id"]])}
                    for r in _read(books / "payments.csv")]
    month = window["last_month"]
    as_of = tr.month_end(month)
    rows: list[dict] = []
    for (customer, bucket), cents in sorted(aging(invoices, applications, as_of, policy["aging_buckets_days"]).items()):
        rows.append({"table": "aging", "customer_id": customer, "bucket": bucket, "open_cents": cents})
    stats = days_to_pay(invoices, applications, as_of, policy["dtp_window_months"], policy["dtp_min_paid_invoices"])
    for customer, (paid, median, p80, fallback) in sorted(stats.items()):
        rows.append({"table": "dtp", "customer_id": customer, "paid_invoices": paid, "median_days": median,
                     "p80_days": p80, "fallback": _flag(fallback)})
    for row in expected_cash(invoices, applications, as_of, window_months=policy["dtp_window_months"],
                             min_paid=policy["dtp_min_paid_invoices"], at_risk_days=policy["at_risk_days_past_due"],
                             calendar=calendar):
        rows.append({"table": "expected_cash", "customer_id": row["customer_id"], "invoice_id": row["invoice_id"],
                     "case": row["case"], "expected_date": row["expected_date"].isoformat(),
                     "cents": row["cents"], "at_risk": _flag(row["at_risk"])})
    return month, rows


def business_days(path: Path) -> list[date]:
    return sorted(date.fromisoformat(r["date"]) for r in _read(path) if r["is_business_day"] == "1")


def expected(books: Path, truth: Path, config: Path) -> dict[str, str]:
    """The text of each expected file, by file name. The calendar is the committed reference one."""
    calendar = business_days(Path(__file__).resolve().parents[3] / "data" / "reference" / "business_days.csv")
    month, rows = rows_for(books, truth, config, calendar)
    out = io.StringIO()
    writer = csv.writer(out, lineterminator="\n")
    writer.writerow(COLUMNS)
    writer.writerows([row.get(c, "") for c in COLUMNS] for row in rows)
    return {f"collections_{month}.csv": out.getvalue()}
