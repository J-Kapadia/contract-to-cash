"""The general ledger, posted from invoice, payment and bank rows.

`gl(invoices, payments, bank, opex, months)` returns the `gl.csv` rows (dicts keyed by the
file's columns, signed `amount_cents`, debit positive). Every entry balances and carries one
`source_ref`. The module is pure and every amount is an integer of cents.

Postings:
    opening cash       the first bank row: Dr 1000 / Cr 3000 (equity), on its posting date
    invoice line       Dr 1200 / Cr 2400 for a subscription or credit line, Cr 4100 for an
                       overage line, on `invoice_date`, `source_ref` the invoice id
    recognition        Dr 2400 / Cr 4000 per month of a subscription or credit line's service
                       period, the line's `ratable_cents` for that month, on the month's last
                       day, `source_ref` `rec:<invoice id>:<line no>`; only for `months`
    payment            Dr 1000 / Cr 1200 of `applied_cents` on `applied_on`, `source_ref` the
                       payment id
    disbursement       Dr 6xxx / Cr 1000 for each negative bank row after the first, on its
                       posting date, the account and name from `opex.expenses`

`gl_period` is the month of the posting date. `subledger_name` is the customer's name in the
ledger (`subledger_names` builds the map from the plan); it is blank on the opening entry and
the vendor or payee name on a disbursement. A line item with a signed (credit) amount posts the
same legs with the sign carried through, so a credit line reduces 1200 and 2400.

Choices made where the brief is silent: an entry is one invoice line, one payment row or one
disbursement; entry ids are `je_000001`.. in posting-date order, with invoices before payments
before disbursements before recognition on a day; a bank receipt needs no posting of its own
(the payment row that applies it posts it); a negative bank row that `opex` does not know is an
error rather than a guess at its account; `known_on` is the posting date.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping

from c2c import truth_reference as tr

from .opex import Opex
from .plan import Plan
from .seeds import rng as default_rng
from .variants import variants

GL_COLUMNS = (
    "entry_id", "line_no", "posted_on", "gl_period", "gl_account", "amount_cents",
    "subledger_name", "source_ref", "known_on",
)
SUBSCRIPTION_LINES = ("subscription", "credit")
REVENUE_ACCOUNTS = {"subscription": "4000", "credit": "4000", "overage": "4100"}

CASH, AR, DEFERRED, EQUITY = "1000", "1200", "2400", "3000"

# Order of kinds of entry within a day.
INVOICE, PAYMENT, DISBURSEMENT, RECOGNITION, OPENING = 1, 2, 3, 4, 0


def subledger_names(plan: Plan, rng=default_rng) -> dict[str, str]:
    """`billing_customer_id` -> the customer's name in the ledger, for `gl(subledger_names=...)`."""
    v = variants(plan, rng)
    return {bc: v.name_for(acct, "gl") for acct, bc in v.billing_customer_ids.items()}


def gl(invoices: Iterable[Mapping], payments: Iterable[Mapping], bank: Iterable[Mapping], opex: Opex,
       months: Iterable[str], *, subledger_names: Mapping[str, str] | None = None) -> list[dict]:
    """The ledger rows for the given books; `months` are the months recognition is posted for."""
    names = subledger_names or {}
    window = sorted(set(months))
    in_window = set(window)

    def name_of(customer: str) -> str:
        return names.get(customer, customer)

    # (posted_on, kind, source_ref, [(account, cents, subledger)])
    entries: list[tuple[str, int, str, list[tuple[str, int, str]]]] = []

    bank_rows = list(bank)
    if bank_rows:
        opening = bank_rows[0]
        entries.append((str(opening["posted_on"]), OPENING, str(opening["bank_txn_id"]),
                        [(CASH, int(opening["amount_cents"]), ""), (EQUITY, -int(opening["amount_cents"]), "")]))
    for row in bank_rows[1:]:
        cents = int(row["amount_cents"])
        if cents >= 0:
            continue
        txn = str(row["bank_txn_id"])
        if txn not in opex.expenses:
            raise ValueError(f"bank disbursement {txn} ({cents} cents) is not an operating expense: no ledger account for it")
        account, payee = opex.expenses[txn]
        entries.append((str(row["posted_on"]), DISBURSEMENT, txn, [(account, -cents, payee), (CASH, cents, payee)]))

    for row in invoices:
        kind, cents, customer = row["line_type"], int(row["amount_cents"]), name_of(str(row["billing_customer_id"]))
        if kind not in REVENUE_ACCOUNTS:
            raise ValueError(f"unknown invoice line_type {kind!r}")
        invoice_id = str(row["invoice_id"])
        credit_account = DEFERRED if kind in SUBSCRIPTION_LINES else REVENUE_ACCOUNTS[kind]
        entries.append((str(row["invoice_date"]), INVOICE, invoice_id,
                        [(AR, cents, customer), (credit_account, -cents, customer)]))
        if kind in SUBSCRIPTION_LINES:
            start, end = str(row["service_start"]), str(row["service_end"])
            for month in tr.months_between(tr.month_of(start), tr.month_of(end)):
                share = tr.ratable_cents(cents, start, end, month)
                if month in in_window and share:
                    entries.append((tr.month_end(month).isoformat(), RECOGNITION, f"rec:{invoice_id}:{row['line_no']}",
                                    [(DEFERRED, share, customer), (REVENUE_ACCOUNTS[kind], -share, customer)]))

    for row in payments:
        cents, customer = int(row["applied_cents"]), name_of(str(row["billing_customer_id"]))
        entries.append((str(row["applied_on"]), PAYMENT, str(row["payment_id"]),
                        [(CASH, cents, customer), (AR, -cents, customer)]))

    out = []
    for n, (day, _, source_ref, legs) in enumerate(sorted(entries, key=lambda e: e[:3]), start=1):
        if sum(cents for _, cents, _ in legs):
            raise ValueError(f"entry for {source_ref} does not balance")
        for line_no, (account, cents, subledger) in enumerate(legs, start=1):
            out.append({"entry_id": f"je_{n:06d}", "line_no": line_no, "posted_on": day, "gl_period": day[:7],
                        "gl_account": account, "amount_cents": cents, "subledger_name": subledger,
                        "source_ref": source_ref, "known_on": day})
    return out
