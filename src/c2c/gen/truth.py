"""The answer key: what the books should be found to say, and what the future holds.

`truth(plan, books_rows, future_rows)` returns the text of every file of the preset's truth
directory, by file name. It is pure: it renders rows the stages already produced and computes
the expected roll-forwards through `c2c.truth_reference`, the independent implementation of
the money arithmetic, so a test that compares the warehouse or the ledger with these files
compares two implementations.

    identities.csv            source_system, source_id, true_customer_id: the customer behind
                              every CRM account and billing customer id (`crm`, `billing`) and
                              behind every spelling of a name in the CRM (`crm_name`), billing
                              (`billing_name`), ledger (`gl_name`) and bank (`bank_name`)
    collection_behaviour.csv  per customer, the days-to-pay parameters the cash stage draws from
    expected_rollforward.csv  per customer and month from the first month with a row through the
                              last books month: revenue, the deferred revenue and receivable
                              roll-forwards (opening, additions, reductions, closing) and the
                              cash applied to the customer's invoices
    future_invoices.csv       the invoice lines dated after the books, through the truth horizon
    future_cash.csv           one row per day of the truth horizon: receipts, disbursements, net
                              and the closing bank balance
    renewal_outcomes.csv      what happened at each contract expiry, through the truth horizon
    matches_expected.csv      the true decision for each designed doubt pair
    planted_problems.json     for each injected error, the books row the injection targets

`books_rows` maps a books file stem to its rows (`crm_accounts`, `billing_customers`,
`contracts`, `invoices`, `payments`, `bank_transactions`, `gl`) and `future_rows` maps
`invoices`, `bank_transactions` and `renewal_outcomes` to the rows past the books; the future
rows are truth only and never reach the books. Rows are dicts keyed by column name.

Choices made where the brief is silent: a doubt pair is written as the CRM account and the
billing customer the matcher is unsure about; the daily rows cover every calendar day of the
horizon, zero on a day with no movement; a preset with no truth horizon has header-only future
files and a preset with no injection month has no planted problems; an injected error targets a
row of the customer the plan chose for it, or of any customer when that customer has none in
the injection month, picked from the candidates in key order by the error's own stream.
"""

from __future__ import annotations

import csv
import io
import json
import random
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from datetime import timedelta

from c2c import truth_reference as tr

from .billing import INVOICE_COLUMNS
from .cash import BEHAVIOUR_COLUMNS
from .plan import INJECT_ERRORS, Plan
from .seeds import rng as default_rng
from .variants import variants

Rows = Sequence[Mapping]

IDENTITY_COLUMNS = ("source_system", "source_id", "true_customer_id")
OUTCOME_COLUMNS = ("contract_id", "expiry", "outcome", "acv_before", "acv_after")
ROLLFORWARD_COLUMNS = (
    "customer_id", "month", "revenue_cents",
    "defrev_opening_cents", "defrev_additions_cents", "defrev_reductions_cents", "defrev_closing_cents",
    "ar_opening_cents", "ar_additions_cents", "ar_reductions_cents", "ar_closing_cents",
    "cash_applied_cents",
)
FUTURE_CASH_COLUMNS = ("date", "receipts_cents", "disbursements_cents", "net_cents", "closing_cents")
MATCH_COLUMNS = ("pair_id", "source_a", "source_b", "decision")

# file stem -> (columns, sort key columns)
FILES = {
    "identities": (IDENTITY_COLUMNS, ("source_system", "source_id")),
    "collection_behaviour": (BEHAVIOUR_COLUMNS, ("customer_id",)),
    "expected_rollforward": (ROLLFORWARD_COLUMNS, ("customer_id", "month")),
    "future_invoices": (INVOICE_COLUMNS, ("invoice_id", "line_no")),
    "future_cash": (FUTURE_CASH_COLUMNS, ("date",)),
    "renewal_outcomes": (OUTCOME_COLUMNS, ("contract_id", "expiry")),
    "matches_expected": (MATCH_COLUMNS, ("pair_id",)),
}
PLANTED = "planted_problems.json"


def render_csv(columns: tuple[str, ...], key: tuple[str, ...], rows: Iterable[Mapping]) -> str:
    """The text of a CSV file: LF line endings, one header row, rows sorted by `key`."""
    out = io.StringIO()
    writer = csv.writer(out, lineterminator="\n")
    writer.writerow(columns)
    writer.writerows([row[c] for c in columns] for row in sorted(rows, key=lambda r: tuple(r[k] for k in key)))
    return out.getvalue()


def _identities(plan: Plan) -> list[dict]:
    v = variants(plan, default_rng)
    rows = []
    for c in plan.customers:
        acct = c.crm_account_id
        rows.append({"source_system": "crm", "source_id": acct, "true_customer_id": acct})
        rows.append({"source_system": "billing", "source_id": v.billing_customer_ids[acct], "true_customer_id": acct})
        for system in ("crm", "billing", "gl", "bank"):
            rows.append({"source_system": f"{system}_name", "source_id": v.name_for(acct, system),
                         "true_customer_id": acct})
    return rows


def _expected_rollforward(plan: Plan, invoices: Rows, payments: Rows) -> list[dict]:
    customer_of = {bc: acct for acct, bc in variants(plan, default_rng).billing_customer_ids.items()}
    inv = [{**r, "customer_id": customer_of[r["billing_customer_id"]]} for r in invoices]
    pay = [{**r, "customer_id": customer_of[r["billing_customer_id"]]} for r in payments]
    rolls = tr.rollforward(inv, pay, through=plan.last_month, customer_key="customer_id")
    cash: dict[tuple[str, str], int] = defaultdict(int)
    for r in pay:
        cash[r["customer_id"], tr.month_of(r["applied_on"])] += int(r["applied_cents"])
    revenue: dict[tuple[str, str], int] = defaultdict(int)
    for (cust, month, _), cents in rolls.revenue.items():
        revenue[cust, month] += cents
    rows = []
    for (cust, month), defrev in sorted(rolls.defrev.items()):
        ar = rolls.ar[cust, month]
        rows.append({
            "customer_id": cust, "month": month, "revenue_cents": revenue[cust, month],
            "defrev_opening_cents": defrev.opening, "defrev_additions_cents": defrev.additions,
            "defrev_reductions_cents": defrev.reductions, "defrev_closing_cents": defrev.closing,
            "ar_opening_cents": ar.opening, "ar_additions_cents": ar.additions,
            "ar_reductions_cents": ar.reductions, "ar_closing_cents": ar.closing,
            "cash_applied_cents": cash[cust, month],
        })
    return rows


def _future_cash(plan: Plan, books_bank: Rows, future_bank: Rows) -> list[dict]:
    if not plan.truth_last_month:
        return []
    balance = sum(int(r["amount_cents"]) for r in books_bank)
    receipts: dict[str, int] = defaultdict(int)
    disbursements: dict[str, int] = defaultdict(int)
    for r in future_bank:
        cents = int(r["amount_cents"])
        if cents >= 0:
            receipts[r["posted_on"]] += cents
        else:
            disbursements[r["posted_on"]] += -cents
    rows = []
    day, last = tr.month_end(plan.last_month) + timedelta(days=1), tr.month_end(plan.truth_last_month)
    while day <= last:
        key = day.isoformat()
        balance += receipts[key] - disbursements[key]
        rows.append({"date": key, "receipts_cents": receipts[key], "disbursements_cents": disbursements[key],
                     "net_cents": receipts[key] - disbursements[key], "closing_cents": balance})
        day += timedelta(days=1)
    return rows


def _matches(plan: Plan) -> list[dict]:
    return [{"pair_id": d.pair_id, "source_a": d.crm_account_id, "source_b": d.billing_customer_id,
             "decision": d.decision} for d in variants(plan, default_rng).doubts]


def _customer_of_refs(plan: Plan, books: Mapping[str, Rows]) -> dict[str, str]:
    """The customer behind each invoice id and payment id, the references a ledger entry carries."""
    customer_of = {bc: acct for acct, bc in variants(plan, default_rng).billing_customer_ids.items()}
    refs = {r["invoice_id"]: customer_of[r["billing_customer_id"]] for r in books["invoices"]}
    refs.update({r["payment_id"]: customer_of[r["billing_customer_id"]] for r in books["payments"]})
    return refs


def _ledger_customer(refs: Mapping[str, str], source_ref: str) -> str:
    return refs.get(source_ref.split(":")[1] if source_ref.startswith("rec:") else source_ref, "")


def _candidates(plan: Plan, books: Mapping[str, Rows], month: str) -> dict[str, tuple[str, list[tuple[str, dict]]]]:
    """Per error: the table its injection targets and the (customer, key) of each candidate row."""
    refs = _customer_of_refs(plan, books)
    customer_of = {bc: acct for acct, bc in variants(plan, default_rng).billing_customer_ids.items()}
    first, last = tr.month_start(month).isoformat(), tr.month_end(month).isoformat()
    in_month = lambda day: first <= str(day) <= last

    invoices = {r["invoice_id"]: customer_of[r["billing_customer_id"]]
                for r in books["invoices"] if in_month(r["invoice_date"])}
    billed = {r["billing_customer_id"] for r in books["invoices"] if in_month(r["invoice_date"])}
    gl = [r for r in books["gl"] if r["gl_period"] == month]
    return {
        "duplicate_customer": ("billing_customers", [
            (acct, {"billing_customer_id": bc}) for bc, acct in customer_of.items() if bc in billed]),
        "wrong_period": ("invoices", [(acct, {"invoice_id": i}) for i, acct in invoices.items()]),
        "misapplied_payment": ("payments", [
            (customer_of[r["billing_customer_id"]], {"payment_id": r["payment_id"]})
            for r in books["payments"] if in_month(r["applied_on"])]),
        "unbilled_amendment": ("contracts", [
            (r["crm_account_id"], {"contract_id": r["contract_id"]})
            for r in books["contracts"]
            if r["version"] == 1 and r["term_start"] <= last and r["term_end"] >= first]),
        "defrev_gl_slip": ("gl", [
            (_ledger_customer(refs, r["source_ref"]), {"entry_id": r["entry_id"], "line_no": r["line_no"]})
            for r in gl if r["gl_account"] == "2400" and r["source_ref"].startswith("rec:")]),
        "orphan_bank_txn": ("bank_transactions", [
            (_bank_customer(plan, books, r), {"bank_txn_id": r["bank_txn_id"]})
            for r in list(books["bank_transactions"])[1:] if int(r["amount_cents"]) > 0 and in_month(r["posted_on"])]),
        "rounding_slip": ("gl", [
            (_ledger_customer(refs, r["source_ref"]), {"entry_id": r["entry_id"], "line_no": r["line_no"]})
            for r in gl if r["gl_account"] == "1200"]),
    }


def _bank_customer(plan: Plan, books: Mapping[str, Rows], row: Mapping) -> str:
    paid = {p["bank_txn_id"]: p["billing_customer_id"] for p in books["payments"]}
    customer_of = {bc: acct for acct, bc in variants(plan, default_rng).billing_customer_ids.items()}
    return customer_of.get(paid.get(row["bank_txn_id"], ""), "")


def planted_problems(plan: Plan, books: Mapping[str, Rows], month: str | None) -> dict:
    """For each injected error, the books row its injection targets in `month`."""
    errors: dict[str, dict] = {}
    if month:
        table_rows = _candidates(plan, books, month)
        for error in INJECT_ERRORS:
            table, candidates = table_rows[error]
            holders = set(plan.roles.get(f"inject_{error}", ()))
            own = [c for c in candidates if c[0] in holders]
            pool = sorted(own or candidates, key=lambda c: tuple(str(v) for v in c[1].values()))
            if not pool:
                raise ValueError(f"no row of {table} in {month} can carry the {error} error")
            customer, key = random.Random(f"{plan.seed}:inject:{error}").choice(pool)
            errors[error] = {"table": table, "key": key, "customer": customer, "month": month}
    return {"seed": plan.seed, "preset": plan.preset, "inject_month": month, "errors": errors}


def truth(plan: Plan, books_rows: Mapping[str, Rows], future_rows: Mapping[str, Rows],
          inject_month: str | None = None) -> dict[str, str]:
    """The text of every truth file, by file name; `inject_month` is the preset's injection month."""
    by_stem: dict[str, list[dict]] = {
        "identities": _identities(plan),
        "collection_behaviour": [
            {"customer_id": c.crm_account_id, "median_days": c.dtp.median_days, "spread_days": c.dtp.spread_days,
             "late_share_bps": c.dtp.late_share_bps, "late_extra_days": c.dtp.late_extra_days}
            for c in plan.customers],
        "expected_rollforward": _expected_rollforward(plan, books_rows["invoices"], books_rows["payments"]),
        "future_invoices": list(future_rows["invoices"]),
        "future_cash": _future_cash(plan, books_rows["bank_transactions"], future_rows["bank_transactions"]),
        "renewal_outcomes": list(future_rows["renewal_outcomes"]),
        "matches_expected": _matches(plan),
    }
    files = {f"{stem}.csv": render_csv(*FILES[stem], rows) for stem, rows in by_stem.items()}
    files[PLANTED] = json.dumps(planted_problems(plan, books_rows, inject_month), indent=2, sort_keys=True) + "\n"
    return files
