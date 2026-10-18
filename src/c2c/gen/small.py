"""The `small` preset: three customers, 2026-01 through 2026-03, no name variants.

Three annual contracts are invoiced on 2026-01-01 on 30-day terms and paid in February;
one customer pays only part of its invoice, the rest in March. The GL carries invoices
(Dr 1200 / Cr 2400), payments (Dr 1000 / Cr 1200) and the opening cash balance against
equity (3000), so GL 1200 and GL 1000 tie to the invoices, payments and bank. Nothing is
recognised as revenue yet. Ids are stable: `acct_0001`..`acct_0003`.
"""

from __future__ import annotations

import csv
import json
import random
from datetime import date, timedelta
from pathlib import Path

FIRST_MONTH = "2026-01"
LAST_MONTH = "2026-03"
OPENING_CASH_CENTS = 250_000_000
INVOICE_DATE = date(2026, 1, 1)
TERMS_DAYS = 30

CUSTOMERS = (
    ("Alder Analytics Inc", "alderanalytics.example"),
    ("Birch Logistics Inc", "birchlogistics.example"),
    ("Cedar Health Inc", "cedarhealth.example"),
)
# (month, share of the invoice in basis points) paid per customer; the third pays in two parts.
PAYMENT_PLANS = (
    ((2, 10_000),),
    ((2, 10_000),),
    ((2, 4_000), (3, 3_500)),
)


def _rng(seed: int, entity: str, purpose: str) -> random.Random:
    return random.Random(f"{seed}:{entity}:{purpose}")


def _weekday(rng: random.Random, year: int, month: int) -> date:
    days = [date(year, month, d) for d in range(1, 29)]
    return rng.choice([d for d in days if d.weekday() < 5])


def _write_csv(path: Path, header: list[str], rows: list[list]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.writer(fh, lineterminator="\n")
        writer.writerow(header)
        writer.writerows(rows)


def write(seed: int, out: Path) -> None:
    books = out / "books" / "small"
    truth = out / "truth" / "small"

    accounts, contracts, lines, invoices, payments, bank, gl = [], [], [], [], [], [], []
    bank.append(["bk_0001", INVOICE_DATE.isoformat(), OPENING_CASH_CENTS, "OPENING BALANCE",
                 "opening balance", INVOICE_DATE.isoformat()])
    gl_entries: list[tuple[date, str, list[tuple[str, int, str]]]] = [
        (INVOICE_DATE, "opening", [("1000", OPENING_CASH_CENTS, ""), ("3000", -OPENING_CASH_CENTS, "")]),
    ]
    identities = []
    cash_events: list[tuple[date, int, int, str, int, str, str]] = []  # date, customer, part, invoice, cents, bc, name

    for i, (name, domain) in enumerate(CUSTOMERS, start=1):
        acct, ctr, bc = f"acct_{i:04d}", f"ctr_{i:04d}", f"bc_{i:04d}"
        inv, line, cust = f"inv_{i:04d}", f"ln_{i:04d}_1", f"cust_{i:04d}"
        rng = _rng(seed, acct, "contract")
        signed_on = date(2025, 12, rng.randint(1, 19))
        fee = rng.randrange(60, 240) * 100_000
        created_on = signed_on - timedelta(days=rng.randint(20, 90))
        accounts.append([acct, name, domain, f"rep_{rng.randint(1, 3):02d}",
                         created_on.isoformat(), created_on.isoformat()])
        contracts.append([ctr, 1, acct, signed_on.isoformat(), "2026-01-01", "2026-01-01", "2026-12-31",
                          "annual", TERMS_DAYS, 0, signed_on.isoformat()])
        lines.append([ctr, 1, line, "subscription", fee, 0, 0, signed_on.isoformat()])
        due = INVOICE_DATE + timedelta(days=TERMS_DAYS)
        invoices.append([inv, 1, bc, ctr, line, INVOICE_DATE.isoformat(), FIRST_MONTH, due.isoformat(),
                         "2026-01-01", "2026-12-31", "subscription", fee, INVOICE_DATE.isoformat()])
        gl_entries.append((INVOICE_DATE, inv, [("1200", fee, name), ("2400", -fee, name)]))
        identities += [["crm", acct, cust], ["billing", bc, cust]]

        prng = _rng(seed, acct, "payment")
        for k, (month, bps) in enumerate(PAYMENT_PLANS[i - 1], start=1):
            day = _weekday(prng, 2026, month)
            cash_events.append((day, i, k, inv, fee * bps // 10_000, bc, name))

    # Bank and payment ids follow the date order of the cash, so they do not depend on customer order.
    for n, (day, _, _, inv, cents, bc, name) in enumerate(sorted(cash_events), start=1):
        pay, bk = f"pay_{n:04d}", f"bk_{n + 1:04d}"
        payments.append([pay, bk, bc, inv, cents, day.isoformat(), day.isoformat()])
        bank.append([bk, day.isoformat(), cents, name.upper(), inv, day.isoformat()])
        gl_entries.append((day, pay, [("1000", cents, name), ("1200", -cents, name)]))

    for n, (day, source_ref, legs) in enumerate(sorted(gl_entries, key=lambda e: e[0]), start=1):
        for line_no, (account, cents, subledger) in enumerate(legs, start=1):
            gl.append([f"je_{n:04d}", line_no, day.isoformat(), day.isoformat()[:7], account, cents,
                       subledger, source_ref, day.isoformat()])

    _write_csv(books / "crm_accounts.csv",
               ["crm_account_id", "account_name", "website_domain", "owner_rep_id", "created_on", "known_on"],
               accounts)
    _write_csv(books / "contracts.csv",
               ["contract_id", "version", "crm_account_id", "signed_on", "effective_from", "term_start",
                "term_end", "billing_frequency", "payment_terms_days", "renewal_number", "known_on"],
               contracts)
    _write_csv(books / "contract_lines.csv",
               ["contract_id", "version", "line_id", "product", "annual_fee_cents", "included_units",
                "overage_cents_per_unit", "known_on"], lines)
    _write_csv(books / "invoices.csv",
               ["invoice_id", "line_no", "billing_customer_id", "contract_id", "contract_line_id",
                "invoice_date", "gl_period", "due_date", "service_start", "service_end", "line_type",
                "amount_cents", "known_on"], invoices)
    _write_csv(books / "payments.csv",
               ["payment_id", "bank_txn_id", "billing_customer_id", "invoice_id", "applied_cents",
                "applied_on", "known_on"], payments)
    _write_csv(books / "bank_transactions.csv",
               ["bank_txn_id", "posted_on", "amount_cents", "counterparty_text", "reference_text", "known_on"],
               bank)
    _write_csv(books / "gl.csv",
               ["entry_id", "line_no", "posted_on", "gl_period", "gl_account", "amount_cents",
                "subledger_name", "source_ref", "known_on"], gl)
    (books / "window.json").write_text(
        json.dumps({"first_month": FIRST_MONTH, "last_month": LAST_MONTH}, separators=(",", ":")) + "\n",
        encoding="utf-8", newline="\n")
    _write_csv(truth / "identities.csv", ["source_system", "source_id", "true_customer_id"], identities)
