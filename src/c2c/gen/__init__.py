"""Books and truth generation, reproducible from a seed.

`generate(preset, seed, out)` plans the customers, runs the stages in order (variants,
contracts, billing, cash, pipeline, operating costs, ledger) and writes the books, the
answer key and the manifest; `verify(out)` lists the files that no longer match a manifest.

Where the files go: the `full` and `mini` presets write `<out>/books/` and `<out>/truth/` with
their manifest at `<out>/books.manifest.sha256` (`mini` is the preset committed under
`tests/fixtures/`, one preset to a directory); any other preset writes `<out>/books/<preset>/`,
`<out>/truth/<preset>/` and `<out>/books/<preset>.manifest.sha256` (see `manifest`).

Every books file is UTF-8 with LF line endings, one header row, rows sorted by the file's key
and a `known_on` on every row. `window.json` gives the first and last month of the books.

The answer key (see `truth`) also looks past the books: the contract, billing, cash and cost stages
are run on to the end of the truth horizon, and the invoices and bank rows after the books' last
month become `future_invoices.csv` and `future_cash.csv`, never books.

Choices made where the brief is silent: the commission stage reads every first contract version
as a booking and the payments on an invoice with a line in the first twelve months of its
contract as collections; a contract row, and the lines of a version, are known on the day that
version is signed; the books come from the stages run on the books' window alone, so the horizon
never changes them; past the window a customer pays every invoice in one receipt (the planted
payment shapes are inside the books), and the receipts after the window are those of the invoices
the books leave unpaid or issue later.
"""

from __future__ import annotations

import csv
import dataclasses
import json
import re
from pathlib import Path

from c2c.truth_reference import month_end

from . import manifest
from .billing import INVOICE_COLUMNS, USAGE_COLUMNS, billing
from .cash import BANK_COLUMNS, PAYMENT_COLUMNS, cash
from .contracts import contracts
from .gl import GL_COLUMNS, gl, subledger_names
from .opex import BILL_COLUMNS, COMMISSION_COLUMNS, COMMITMENT_COLUMNS, HEADCOUNT_COLUMNS, PAYROLL_COLUMNS, opex
from .pipeline import PIPELINE_COLUMNS, pipeline
from .plan import CONFIG_PATH, Plan, build_plan, load_config
from .seeds import rng
from .truth import FILES as TRUTH
from .truth import truth
from .variants import variants

PRESETS = ("small", "mini", manifest.FULL)

CRM_ACCOUNT_COLUMNS = ("crm_account_id", "account_name", "website_domain", "owner_rep_id", "created_on", "known_on")
CRM_CONTACT_COLUMNS = ("contact_id", "crm_account_id", "email", "role", "known_on")
BILLING_CUSTOMER_COLUMNS = ("billing_customer_id", "bill_to_name", "bill_to_email", "crm_account_ref", "known_on")
CONTRACT_COLUMNS = ("contract_id", "version", "crm_account_id", "signed_on", "effective_from", "term_start",
                    "term_end", "billing_frequency", "payment_terms_days", "renewal_number", "known_on")
CONTRACT_LINE_COLUMNS = ("contract_id", "version", "line_id", "product", "annual_fee_cents", "included_units",
                         "overage_cents_per_unit", "known_on")

# file stem -> (columns, sort key columns)
BOOKS = {
    "crm_accounts": (CRM_ACCOUNT_COLUMNS, ("crm_account_id",)),
    "crm_contacts": (CRM_CONTACT_COLUMNS, ("contact_id",)),
    "pipeline_snapshots": (PIPELINE_COLUMNS, ("snapshot_date", "opp_id")),
    "contracts": (CONTRACT_COLUMNS, ("contract_id", "version")),
    "contract_lines": (CONTRACT_LINE_COLUMNS, ("contract_id", "version", "line_id")),
    "usage_monthly": (USAGE_COLUMNS, ("contract_id", "month")),
    "billing_customers": (BILLING_CUSTOMER_COLUMNS, ("billing_customer_id",)),
    "invoices": (INVOICE_COLUMNS, ("invoice_id", "line_no")),
    "payments": (PAYMENT_COLUMNS, ("payment_id",)),
    "bank_transactions": (BANK_COLUMNS, ("bank_txn_id",)),
    "gl": (GL_COLUMNS, ("entry_id", "line_no")),
    "payroll_runs": (PAYROLL_COLUMNS, ("pay_date", "half")),
    "headcount_plan": (HEADCOUNT_COLUMNS, ("month",)),
    "commission_plan": (COMMISSION_COLUMNS, ("effective_from",)),
    "ap_bills": (BILL_COLUMNS, ("bill_id",)),
    "vendor_commitments": (COMMITMENT_COLUMNS, ("commitment_id",)),
}
PLANTED_PROBLEMS = "planted_problems.json"
FIRST_YEAR_MONTHS = 12
PAYMENT_SHAPES = ("partial_payment", "overpayment", "multi_payment")


def _write_csv(path: Path, columns: tuple[str, ...], key: tuple[str, ...], rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.writer(fh, lineterminator="\n")
        writer.writerow(columns)
        writer.writerows([row[c] for c in columns] for row in sorted(rows, key=lambda r: tuple(r[k] for k in key)))


def _known_on(contract_rows: list[dict], line_rows: list[dict]) -> tuple[list[dict], list[dict]]:
    """Contract and line rows with `known_on`, the day their version is signed."""
    signed = {(r["contract_id"], r["version"]): r["signed_on"] for r in contract_rows}
    return ([{**r, "known_on": r["signed_on"]} for r in contract_rows],
            [{**r, "known_on": signed[(r["contract_id"], r["version"])]} for r in line_rows])


def _bookings(contract_rows: list[dict], line_rows: list[dict]) -> list[dict]:
    fee = {(r["contract_id"], r["version"]): r["annual_fee_cents"] for r in line_rows if r["product"] == "subscription"}
    return [{"signed_on": r["signed_on"], "acv_cents": fee[(r["contract_id"], 1)]}
            for r in contract_rows if r["version"] == 1]


def _first_year_collections(contract_rows: list[dict], invoices: list[dict], payments: list[dict]) -> list[dict]:
    term_start = {r["contract_id"]: r["term_start"] for r in contract_rows}
    cutoff = {}
    for cid, start in term_start.items():
        idx = int(start[:4]) * 12 + int(start[5:7]) - 1 + FIRST_YEAR_MONTHS
        cutoff[cid] = f"{idx // 12:04d}-{idx % 12 + 1:02d}-01"
    first_year = {i["invoice_id"] for i in invoices if i["service_start"] < cutoff[i["contract_id"]]}
    return [p for p in payments if p["invoice_id"] in first_year]


def _stages(plan: Plan) -> tuple[dict[str, list[dict]], list[dict]]:
    """The books of `plan` by file stem, and the renewal outcomes."""
    v = variants(plan, rng)
    contract_rows, line_rows, outcomes = contracts(plan, rng)
    invoices, usage = billing(plan, contract_rows, line_rows, rng)
    payments, receipts, _ = cash(plan, invoices, v.name_for, rng)
    snapshots = pipeline(plan, contract_rows, rng, line_rows)
    costs = opex(plan, _bookings(contract_rows, line_rows),
                 _first_year_collections(contract_rows, invoices, payments), rng)
    bank = sorted(receipts + costs.bank_rows, key=lambda r: r["bank_txn_id"])  # the opening row sorts first
    ledger = gl(invoices, payments, bank, costs, plan.months, subledger_names=subledger_names(plan, rng))
    contract_rows, line_rows = _known_on(contract_rows, line_rows)
    books = {
        "crm_accounts": list(v.crm_accounts), "crm_contacts": list(v.crm_contacts),
        "pipeline_snapshots": snapshots, "contracts": contract_rows, "contract_lines": line_rows,
        "usage_monthly": usage, "billing_customers": list(v.billing_customers), "invoices": invoices,
        "payments": payments, "bank_transactions": bank, "gl": ledger,
        "payroll_runs": costs.payroll_runs, "headcount_plan": costs.headcount_plan,
        "commission_plan": costs.commission_plan, "ap_bills": costs.ap_bills,
        "vendor_commitments": costs.vendor_commitments,
    }
    return books, outcomes


def _future(plan: Plan, books: dict[str, list[dict]]) -> dict[str, list[dict]]:
    """The invoices and bank rows after the books' last month, through the end of the truth horizon.

    The stages run again on the plan stretched to the horizon, which leaves everything inside the
    window as it was. The books already hold the receipts of the planted payment shapes, so the
    stretched plan drops those roles: it pays each invoice in one receipt, and only the receipts
    of invoices the books have not paid are kept (and numbered on from the books' last receipt)."""
    if not plan.truth_last_month:
        return {"invoices": [], "bank_transactions": []}
    wide = dataclasses.replace(
        plan, last_month=plan.truth_last_month, months=plan.months + plan.truth_months,
        roles={role: accts for role, accts in plan.roles.items() if role not in PAYMENT_SHAPES})
    window_end = month_end(plan.last_month).isoformat()

    v = variants(plan, rng)
    contract_rows, line_rows, _ = contracts(wide, rng)
    invoices, _ = billing(wide, contract_rows, line_rows, rng)
    payments, receipts, _ = cash(wide, invoices, v.name_for, rng)

    paid = {p["invoice_id"] for p in books["payments"]}
    open_payments = [p for p in payments if p["invoice_id"] not in paid]
    open_txns = {p["bank_txn_id"] for p in open_payments}
    open_receipts = sorted((r for r in receipts if r["bank_txn_id"] in open_txns),
                           key=lambda r: (r["posted_on"], r["bank_txn_id"]))
    if any(r["posted_on"] <= window_end for r in open_receipts):
        raise ValueError("an invoice the books leave unpaid is paid inside the books' window when the stages run on")
    last_receipt = max(int(m.group(1)) for r in books["bank_transactions"]
                       if (m := re.fullmatch(r"bk_(\d+)", r["bank_txn_id"])))
    later = [{**r, "bank_txn_id": f"bk_{last_receipt + n:06d}"} for n, r in enumerate(open_receipts, start=1)]

    costs = opex(wide, _bookings(contract_rows, line_rows),
                 _first_year_collections(contract_rows, invoices, books["payments"] + open_payments), rng)
    return {
        "invoices": [r for r in invoices if r["invoice_date"] > window_end],
        "bank_transactions": sorted(later + [r for r in costs.bank_rows if r["posted_on"] > window_end],
                                    key=lambda r: (r["posted_on"], r["bank_txn_id"])),
    }


def _clear(directory: Path) -> None:
    """Remove the files directly in `directory`; another preset's subdirectory is left alone."""
    for path in directory.glob("*") if directory.is_dir() else ():
        if path.is_file() and not path.name.endswith(".manifest.sha256"):
            path.unlink()


def generate(preset: str, seed: int, out: str | Path, config: str | Path = CONFIG_PATH) -> manifest.Manifest:
    """Write the preset's books and truth under `out`, replacing any earlier copy, and record the manifest."""
    cfg = load_config(config)
    if preset not in cfg["presets"]:
        raise ValueError(f"unknown preset {preset!r}; available: {', '.join(sorted(cfg['presets']))}")
    out = Path(out)
    plan = build_plan(preset, cfg, seed)
    books, outcomes = _stages(plan)
    future = {**_future(plan, books), "renewal_outcomes": outcomes}
    answer_key = truth(plan, books, future, cfg["presets"][preset].get("inject_month"))

    books_dir, truth_dir = manifest.preset_dirs(out, preset)
    for directory in (books_dir, truth_dir):
        _clear(directory)
    for stem, rows in books.items():
        columns, key = BOOKS[stem]
        _write_csv(books_dir / f"{stem}.csv", columns, key, rows)
    truth_dir.mkdir(parents=True, exist_ok=True)
    for name, text in answer_key.items():
        (truth_dir / name).write_text(text, encoding="utf-8", newline="\n")
    (books_dir / "window.json").write_text(
        json.dumps({"first_month": plan.first_month, "last_month": plan.last_month}, separators=(",", ":")) + "\n",
        encoding="utf-8", newline="\n")

    built = manifest.build(out, preset, seed, Path(config))
    manifest.manifest_path(out, preset).write_text(built.render(), encoding="utf-8", newline="\n")
    return built


def verify(out: str | Path, preset: str | None = None) -> dict[str, list[str]]:
    """Drifted files per preset (empty list: in step). Every manifest under `out` is checked
    unless `preset` names one. Raises FileNotFoundError when there is no manifest to check."""
    out = Path(out)
    presets = [preset] if preset is not None else manifest.presets_under(out)
    if not presets:
        raise FileNotFoundError(f"no books manifest under {out}")
    result = {}
    for name in presets:
        path = manifest.manifest_path(out, name)
        if not path.is_file():
            raise FileNotFoundError(f"no books manifest at {path}")
        if name in manifest.FLAT and (recorded := manifest.read(path).preset) != name:
            raise FileNotFoundError(f"{path} is the manifest of the {recorded} preset, not {name}")
        result[name] = manifest.drift(out, name)
    return result
