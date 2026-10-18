"""Payroll, commissions, vendor bills and commitments, and the bank rows that pay them.

`opex(plan, bookings, collections, rng)` returns an `Opex`: the `payroll_runs.csv`,
`headcount_plan.csv`, `commission_plan.csv`, `ap_bills.csv` and `vendor_commitments.csv` rows
(dicts keyed by the file's columns, every row carrying `known_on`), the bank disbursement rows
that pay them (`bank_transactions.csv` columns, negative amounts, built with the cash stage's
`bank_row`) and `expenses`, the ledger account and subledger name of each disbursement by
`bank_txn_id`. The module is pure: the plan,
the rows and the seeded streams (`rng(seed, entity, purpose)`, as in `seeds.rng`) give the same
rows every time. Every amount is an integer of cents.

Inputs are rows of the books: `bookings` carry `signed_on` and `acv_cents` (the first-year ACV
of a contract signed that day); `collections` carry `applied_on` and `applied_cents` (cash
applied to first-year invoices, so the caller filters payments to those).

Payroll is paid on the 15th and the last day of each month of the books, moved back to the
previous business day when that is not one; a run costs the headcount of its month times the
configured cost per head. Commissions are one payment a month, on the 15th of the month after
(previous business day), of the booking rate on the month's bookings plus the collection rate on
the month's first-year collections. Vendor bills follow the configured cadence; a bill is paid
its payment days after the bill date plus a few days of lateness, on a business day, and stays
unpaid (`paid_bank_txn_id` empty) when that day is past the last day of the books.

Choices made where the brief is silent: expense is recorded when it is paid (no accrual); one
commission rate row, 500 bps on bookings and 100 bps on first-year collections from the first
day of the books; a cloud bill is dated the last day of its month and varies 90 to 110 percent
around the configured amount, every other bill is dated the first; headcount plan rows are the
hires from the first month of the books on, known the first day of the month before the hire;
commitments cover the rent, events and annual software bills over the books and the truth
horizon, known three months ahead (never before the first day of the books); disbursement ids are
`bk_opx_000001`.. in posting-date order, so they cannot collide with the receipt ids of the cash
stage.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import date, timedelta
from random import Random

from c2c.truth_reference import month_end, month_start

from .cash import BANK_COLUMNS, bank_row, next_business_day, previous_business_day
from .plan import Plan
from .seeds import rng as default_rng

Rng = Callable[[int, str, str], Random]

COMMISSION_ON_BOOKING_BPS = 500
COMMISSION_ON_COLLECTION_BPS = 100
CLOUD_LOW_BPS, CLOUD_HIGH_BPS = 9000, 11000
MAX_LATE_DAYS = 3
COMMITMENT_NOTICE_MONTHS = 3

ACCOUNT_PAYROLL, ACCOUNT_COMMISSIONS = "6000", "6100"
CATEGORY_ACCOUNTS = {"cloud": "6200", "vendor": "6300", "software": "6300", "events": "6400", "rent": "6500"}
COMMITTED = ("rent", "events", "software")
QUARTER_START_MONTHS = (1, 4, 7, 10)

PAYROLL_COLUMNS = ("pay_date", "half", "cash_cents", "headcount", "known_on")
HEADCOUNT_COLUMNS = ("month", "delta_cash_per_run_cents", "known_on")
COMMISSION_COLUMNS = ("rate_on_booking_bps", "rate_on_collection_bps", "effective_from", "known_on")
BILL_COLUMNS = ("bill_id", "vendor_name", "category", "bill_date", "due_date", "amount_cents",
                "paid_bank_txn_id", "known_on")
COMMITMENT_COLUMNS = ("commitment_id", "vendor_name", "category", "due_date", "amount_cents", "known_on")


@dataclass(frozen=True)
class Opex:
    payroll_runs: list[dict] = field(default_factory=list)
    headcount_plan: list[dict] = field(default_factory=list)
    commission_plan: list[dict] = field(default_factory=list)
    ap_bills: list[dict] = field(default_factory=list)
    vendor_commitments: list[dict] = field(default_factory=list)
    bank_rows: list[dict] = field(default_factory=list)
    expenses: dict[str, tuple[str, str]] = field(default_factory=dict)  # bank_txn_id -> (gl_account, subledger_name)


def _shift(month: str, months: int) -> str:
    idx = int(month[:4]) * 12 + int(month[5:]) - 1 + months
    return f"{idx // 12:04d}-{idx % 12 + 1:02d}"


def _headcount(plan: Plan, month: str) -> int:
    return plan.headcount["opening"] + sum(h["delta"] for h in plan.headcount["hires"] if h["month"] <= month)


def _bill_months(vendor: dict, months: Iterable[str]) -> list[str]:
    cadence = vendor["cadence"]
    if cadence == "monthly":
        return list(months)
    if cadence == "quarterly":
        return [m for m in months if int(m[5:]) in QUARTER_START_MONTHS]
    return [m for m in months if int(m[5:]) == vendor["bill_month"]]


def _bill_date(vendor: dict, month: str) -> date:
    return month_end(month) if vendor["category"] == "cloud" else month_start(month)


def opex(plan: Plan, bookings: Iterable[dict], collections: Iterable[dict], rng: Rng = default_rng) -> Opex:
    """The operating-cost rows and the bank disbursements that pay them, over the books window."""
    first_day, last_day = month_start(plan.first_month), month_end(plan.last_month)
    cost_per_head = plan.headcount["cost_per_head_per_run_cents"]

    # (posting date, order, source, counterparty, reference, cents, account, subledger name, bill row or None)
    payments: list[tuple] = []

    # Payroll: the 15th and the last day of each month, previous business day.
    payroll_runs = []
    for month in plan.months:
        heads = _headcount(plan, month)
        for half, nominal in (("mid", month_start(month).replace(day=15)), ("end", month_end(month))):
            pay_date = previous_business_day(plan, nominal)
            row = {"pay_date": pay_date.isoformat(), "half": half, "cash_cents": heads * cost_per_head,
                   "headcount": heads, "known_on": pay_date.isoformat()}
            payroll_runs.append(row)
            payments.append((pay_date, 0, f"payroll:{pay_date.isoformat()}", "PAYROLL",
                             f"payroll {half} {pay_date.isoformat()}", row["cash_cents"],
                             ACCOUNT_PAYROLL, "Payroll", None))

    headcount_plan = []
    for hire in plan.headcount["hires"]:
        if hire["month"] < plan.first_month or hire["month"] > (plan.truth_last_month or plan.last_month):
            continue
        known = max(month_start(_shift(hire["month"], -1)), first_day)
        headcount_plan.append({"month": hire["month"], "delta_cash_per_run_cents": hire["delta"] * cost_per_head,
                               "known_on": known.isoformat()})

    commission_plan = [{"rate_on_booking_bps": COMMISSION_ON_BOOKING_BPS,
                        "rate_on_collection_bps": COMMISSION_ON_COLLECTION_BPS,
                        "effective_from": first_day.isoformat(), "known_on": first_day.isoformat()}]

    # Commissions: paid on the 15th of the month after the booking or collection.
    booked: dict[str, int] = defaultdict(int)
    for row in bookings:
        booked[str(row["signed_on"])[:7]] += int(row["acv_cents"])
    collected: dict[str, int] = defaultdict(int)
    for row in collections:
        collected[str(row["applied_on"])[:7]] += int(row["applied_cents"])
    for month in sorted(set(booked) | set(collected)):
        cents = (booked[month] * COMMISSION_ON_BOOKING_BPS // 10_000
                 + collected[month] * COMMISSION_ON_COLLECTION_BPS // 10_000)
        nominal = month_start(_shift(month, 1)).replace(day=15)
        if cents > 0 and first_day <= nominal <= last_day:
            pay_date = previous_business_day(plan, nominal)
            payments.append((pay_date, 1, f"commission:{month}", "SALES COMMISSIONS",
                             f"commissions for {month}", cents, ACCOUNT_COMMISSIONS, "Commissions", None))

    # Vendor bills over the books; each is paid on a business day after its due date.
    bills: list[dict] = []
    for vendor in plan.vendors:
        for month in _bill_months(vendor, plan.months):
            amount = vendor["amount_cents"]
            if vendor["category"] == "cloud":
                amount = amount * rng(plan.seed, vendor["name"], f"bill:{month}").randint(CLOUD_LOW_BPS, CLOUD_HIGH_BPS) // 10_000
            bill_date = _bill_date(vendor, month)
            due = bill_date + timedelta(days=vendor["payment_days"])
            bills.append({"vendor_name": vendor["name"], "category": vendor["category"],
                          "bill_date": bill_date.isoformat(), "due_date": due.isoformat(),
                          "amount_cents": amount, "paid_bank_txn_id": "", "known_on": bill_date.isoformat(),
                          "_late": rng(plan.seed, vendor["name"], f"late:{month}").randint(0, MAX_LATE_DAYS)})
    bills.sort(key=lambda b: (b["bill_date"], b["vendor_name"]))
    for n, bill in enumerate(bills, start=1):
        bill["bill_id"] = f"bill_{n:06d}"
        paid_on = next_business_day(plan, date.fromisoformat(bill["due_date"]) + timedelta(days=bill.pop("_late")))
        if paid_on is not None and paid_on <= last_day:
            payments.append((paid_on, 2, bill["bill_id"], bill["vendor_name"].upper(), bill["bill_id"],
                             bill["amount_cents"], CATEGORY_ACCOUNTS[bill["category"]], bill["vendor_name"], bill))

    # Commitments: the rent, events and annual software bills ahead, over books and truth horizon.
    commitments = []
    span = list(plan.months + plan.truth_months)
    for vendor in plan.vendors:
        if vendor["category"] not in COMMITTED or (vendor["category"] == "software" and vendor["cadence"] != "annual"):
            continue
        for month in _bill_months(vendor, span):
            bill_date = _bill_date(vendor, month)
            known = max(month_start(_shift(month, -COMMITMENT_NOTICE_MONTHS)), first_day)
            commitments.append({"vendor_name": vendor["name"], "category": vendor["category"],
                                "due_date": (bill_date + timedelta(days=vendor["payment_days"])).isoformat(),
                                "amount_cents": vendor["amount_cents"], "known_on": known.isoformat()})
    commitments.sort(key=lambda c: (c["due_date"], c["vendor_name"]))
    for n, row in enumerate(commitments, start=1):
        row["commitment_id"] = f"cmt_{n:06d}"

    bank_rows, expenses = [], {}
    for n, (day, _, _, counterparty, reference, cents, account, subledger, bill) in enumerate(
            sorted(payments, key=lambda p: p[:3]), start=1):
        txn = f"bk_opx_{n:06d}"
        bank_rows.append(bank_row(txn, day, -cents, counterparty, reference))
        expenses[txn] = (account, subledger)
        if bill is not None:
            bill["paid_bank_txn_id"] = txn

    return Opex(
        payroll_runs=payroll_runs,
        headcount_plan=headcount_plan,
        commission_plan=commission_plan,
        ap_bills=[{c: b[c] for c in BILL_COLUMNS} for b in bills],
        vendor_commitments=[{c: r[c] for c in COMMITMENT_COLUMNS} for r in commitments],
        bank_rows=bank_rows,
        expenses=expenses,
    )
