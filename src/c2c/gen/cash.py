"""Payments and bank receipts from invoice rows, and each customer's collection behaviour.

`cash(plan, invoices, name_for, rng)` turns invoice rows (dicts keyed by the `invoices.csv`
columns) into `(payment_rows, bank_rows, collection_behaviour_rows)`: the `payments.csv` and
`bank_transactions.csv` rows, and one row per customer for `truth/collection_behaviour.csv`.
`name_for` is `Variants.name_for`; the module is pure: the plan, the rows and the seeded
streams (`rng(seed, entity, purpose)`, as in `seeds.rng`) give the same rows every time.
Every amount is an integer of cents and every row carries `known_on`, the date it entered the
system (`applied_on` for a payment, `posted_on` for a bank row).

Each customer pays as its plan `dtp` says. An invoice's days to pay are drawn around the
customer's median, plus the late extra with probability `late_share_bps`; the payment date is
the invoice date plus the draw, moved to the next business day, and the receipt lands in the
bank that day. One receipt is one bank row whose counterparty is the customer's bank spelling
and whose reference names the invoice or invoices it pays (the bare invoice id, or the id
after a short prefix, so a remittance reference points at the invoice in either form).

Three shapes are planted for the customers the plan gives the roles to, each once:
`partial_payment` (one invoice paid by two receipts, the second one a few weeks later),
`overpayment` (one receipt exceeds its invoice) and `multi_payment` (one receipt, several
`payments.csv` rows, covers two invoices). Every other invoice with a positive total is paid in
full by one receipt that equals it, so the amount alone identifies the invoice.

The first bank row is the opening balance. Disbursement rows for payroll, bills and
commitments are appended by the opex stage, which builds them with `bank_row`.

Choices made where the brief is silent: an invoice whose lines sum to zero or less (a credit)
is not paid; days to pay are the median plus a uniform offset within the spread, at least one;
nothing posts after the last day of the books, so an invoice not yet paid by then stays open;
the opening balance posts on the first business day of the books for 250,000,000 cents; a
partial first payment is 40 to 70 percent of the invoice (floored) and the rest follows 15 to
45 days later; an overpayment is 5 to 15 percent of the invoice on top of it (at least one
cent); the multi-invoice receipt pays the two consecutive invoices of its customer dated
closest together, on the later one's payment date; payment ids and bank ids number the receipts
in posting order (`pay_000001`.., `bk_000002`.. after the opening balance's `bk_000001`).
"""

from __future__ import annotations

from bisect import bisect_left, bisect_right
from collections import defaultdict
from collections.abc import Callable, Iterable
from datetime import date, timedelta
from random import Random

from c2c.truth_reference import month_end

from .plan import Plan
from .seeds import rng as default_rng
from .variants import variants

Rng = Callable[[int, str, str], Random]
NameFor = Callable[..., str]

OPENING_CASH_CENTS = 250_000_000
PARTIAL_FIRST_BPS = (4000, 7000)
PARTIAL_GAP_DAYS = (15, 45)
OVERPAY_BPS = (500, 1500)
BARE_REFERENCE_BPS = 3000
REFERENCE_PREFIXES = ("PAYMENT", "PMT", "REMITTANCE", "INV")

PAYMENT_COLUMNS = ("payment_id", "bank_txn_id", "billing_customer_id", "invoice_id", "applied_cents",
                   "applied_on", "known_on")
BANK_COLUMNS = ("bank_txn_id", "posted_on", "amount_cents", "counterparty_text", "reference_text", "known_on")
BEHAVIOUR_COLUMNS = ("customer_id", "median_days", "spread_days", "late_share_bps", "late_extra_days")


def bank_row(bank_txn_id: str, posted_on: date | str, amount_cents: int, counterparty_text: str,
             reference_text: str) -> dict:
    """A `bank_transactions.csv` row, known on the day it posts."""
    day = posted_on if isinstance(posted_on, str) else posted_on.isoformat()
    return {"bank_txn_id": bank_txn_id, "posted_on": day, "amount_cents": int(amount_cents),
            "counterparty_text": counterparty_text, "reference_text": reference_text, "known_on": day}


def next_business_day(plan: Plan, day: date) -> date | None:
    """`day` if it is a business day of the plan's calendar, else the next one; None when the
    calendar ends first."""
    i = bisect_left(plan.calendar, day.isoformat())
    return date.fromisoformat(plan.calendar[i]) if i < len(plan.calendar) else None


def previous_business_day(plan: Plan, day: date) -> date | None:
    """`day` if it is a business day of the plan's calendar, else the previous one; None when
    the calendar starts after it."""
    i = bisect_right(plan.calendar, day.isoformat())
    return date.fromisoformat(plan.calendar[i - 1]) if i else None


def _invoices(invoices: Iterable[dict]) -> dict[str, dict]:
    """One entry per invoice id: its customer, date and signed total over its lines."""
    out: dict[str, dict] = {}
    for row in invoices:
        entry = out.setdefault(row["invoice_id"], {
            "invoice_id": row["invoice_id"], "billing_customer_id": row["billing_customer_id"],
            "invoice_date": date.fromisoformat(row["invoice_date"]), "total": 0})
        entry["total"] += int(row["amount_cents"])
    return out


def _days_to_pay(plan: Plan, acct: str, behaviour, invoice_id: str, rng: Rng) -> int:
    r = rng(plan.seed, acct, f"cash:dtp:{invoice_id}")
    days = behaviour.median_days + r.randint(-behaviour.spread_days, behaviour.spread_days)
    if r.randrange(10_000) < behaviour.late_share_bps:
        days += behaviour.late_extra_days
    return max(days, 1)


def _reference(invoice_ids: list[str], r: Random) -> str:
    ids = ", ".join(invoice_ids)
    if r.randrange(10_000) < BARE_REFERENCE_BPS:
        return ids
    return f"{r.choice(REFERENCE_PREFIXES)} {ids}"


def cash(plan: Plan, invoices: Iterable[dict], name_for: NameFor, rng: Rng = default_rng
         ) -> tuple[list[dict], list[dict], list[dict]]:
    """The `payments.csv` and `bank_transactions.csv` rows, and the collection behaviour rows."""
    last_day = month_end(plan.last_month)
    billing_ids = variants(plan, rng).billing_customer_ids
    customer_of = {bc: acct for acct, bc in billing_ids.items()}
    by_customer: dict[str, list[dict]] = defaultdict(list)
    for inv in _invoices(invoices).values():
        if inv["billing_customer_id"] not in customer_of:
            raise ValueError(f"{inv['invoice_id']}: {inv['billing_customer_id']} is not a billing customer of the plan")
        if inv["total"] > 0:
            by_customer[customer_of[inv["billing_customer_id"]]].append(inv)

    def on_bank_day(day: date) -> date | None:
        if day > last_day:
            return None
        posted = next_business_day(plan, day)
        return posted if posted is not None and posted <= last_day else None

    # receipts: (posted, acct, [(invoice_id, applied_cents)]); the bank amount is their sum.
    receipts: list[tuple[date, str, list[tuple[str, int]]]] = []
    for customer in plan.customers:
        acct = customer.crm_account_id
        invs = sorted(by_customer.get(acct, ()), key=lambda i: (i["invoice_date"], i["invoice_id"]))
        for inv in invs:
            inv["pay_day"] = on_bank_day(inv["invoice_date"] + timedelta(days=_days_to_pay(
                plan, acct, customer.dtp, inv["invoice_id"], rng)))
        open_ = [i for i in invs if i["pay_day"] is not None]
        paid_by_shape: set[str] = set()

        def planted(role: str) -> bool:
            return acct in plan.roles.get(role, ())

        def fail(role: str):
            raise ValueError(f"{acct}: no invoice can carry the {role} shape inside the books")

        if planted("multi_payment"):
            pairs = [(a, b) for a, b in zip(invs, invs[1:]) if b["pay_day"] is not None]
            if not pairs:
                fail("multi_payment")
            a, b = min(pairs, key=lambda p: (p[1]["invoice_date"] - p[0]["invoice_date"], p[0]["invoice_date"]))
            receipts.append((b["pay_day"], acct, [(a["invoice_id"], a["total"]), (b["invoice_id"], b["total"])]))
            paid_by_shape |= {a["invoice_id"], b["invoice_id"]}

        if planted("partial_payment"):
            r = rng(plan.seed, acct, "cash:partial")
            choices = []
            for inv in open_:
                if inv["invoice_id"] in paid_by_shape:
                    continue
                second = on_bank_day(inv["pay_day"] + timedelta(days=r.randint(*PARTIAL_GAP_DAYS)))
                if second is not None and second > inv["pay_day"]:
                    choices.append((inv, second))
            if not choices:
                fail("partial_payment")
            inv, second = r.choice(choices)
            first_cents = inv["total"] * r.randint(*PARTIAL_FIRST_BPS) // 10_000
            receipts.append((inv["pay_day"], acct, [(inv["invoice_id"], first_cents)]))
            receipts.append((second, acct, [(inv["invoice_id"], inv["total"] - first_cents)]))
            paid_by_shape.add(inv["invoice_id"])

        if planted("overpayment"):
            r = rng(plan.seed, acct, "cash:overpayment")
            choices = [i for i in open_ if i["invoice_id"] not in paid_by_shape]
            if not choices:
                fail("overpayment")
            inv = r.choice(choices)
            extra = max(inv["total"] * r.randint(*OVERPAY_BPS) // 10_000, 1)
            receipts.append((inv["pay_day"], acct, [(inv["invoice_id"], inv["total"] + extra)]))
            paid_by_shape.add(inv["invoice_id"])

        for inv in open_:
            if inv["invoice_id"] not in paid_by_shape:
                receipts.append((inv["pay_day"], acct, [(inv["invoice_id"], inv["total"])]))

    receipts.sort(key=lambda t: (t[0], t[1], t[2][0][0], t[2][0][1]))
    opening_day = next_business_day(plan, date.fromisoformat(plan.first_month + "-01"))
    bank = [bank_row("bk_000001", opening_day, OPENING_CASH_CENTS, "OPENING BALANCE", "opening balance")]
    payments: list[dict] = []
    n_payment = 0
    for n, (posted, acct, applied) in enumerate(receipts, start=1):
        txn = f"bk_{n + 1:06d}"
        reference = _reference([i for i, _ in applied], rng(plan.seed, acct, f"cash:reference:{txn}"))
        bank.append(bank_row(txn, posted, sum(c for _, c in applied), name_for(acct, "bank"), reference))
        for invoice_id, cents in applied:
            n_payment += 1
            payments.append({
                "payment_id": f"pay_{n_payment:06d}", "bank_txn_id": txn,
                "billing_customer_id": billing_ids[acct], "invoice_id": invoice_id,
                "applied_cents": cents, "applied_on": posted.isoformat(), "known_on": posted.isoformat()})

    behaviour = [{"customer_id": c.crm_account_id, "median_days": c.dtp.median_days,
                  "spread_days": c.dtp.spread_days, "late_share_bps": c.dtp.late_share_bps,
                  "late_extra_days": c.dtp.late_extra_days} for c in plan.customers]
    return payments, bank, behaviour
