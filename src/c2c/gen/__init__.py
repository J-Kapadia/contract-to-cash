"""Books and truth generation, reproducible from a seed.

`generate(preset, seed, out)` plans the customers, runs the stages in order (variants,
contracts, billing, cash, pipeline, operating costs, ledger) and writes the books, the
answer key and the manifest; `verify(out)` lists the files that no longer match a manifest.

Where the files go: the `full` preset writes `<out>/books/` and `<out>/truth/` with its
manifest at `<out>/books.manifest.sha256`; any other preset writes `<out>/books/<preset>/`,
`<out>/truth/<preset>/` and `<out>/books/<preset>.manifest.sha256` (see `manifest`).

Every books file is UTF-8 with LF line endings, one header row, rows sorted by the file's key
and a `known_on` on every row. `window.json` gives the first and last month of the books.

Choices made where the brief is silent: the commission stage reads every first contract version
as a booking and the payments on an invoice with a line in the first twelve months of its
contract as collections; a contract row, and the lines of a version, are known on the day that
version is signed; an identity row is written for each CRM account and billing customer.
"""

from __future__ import annotations

import csv
import json
from pathlib import Path

from . import manifest
from .billing import INVOICE_COLUMNS, USAGE_COLUMNS, billing
from .cash import BANK_COLUMNS, BEHAVIOUR_COLUMNS, PAYMENT_COLUMNS, cash
from .contracts import contracts
from .gl import GL_COLUMNS, gl, subledger_names
from .opex import BILL_COLUMNS, COMMISSION_COLUMNS, COMMITMENT_COLUMNS, HEADCOUNT_COLUMNS, PAYROLL_COLUMNS, opex
from .pipeline import PIPELINE_COLUMNS, pipeline
from .plan import CONFIG_PATH, Plan, build_plan, load_config
from .seeds import rng
from .variants import variants

PRESETS = ("small", "mini", manifest.FULL)

CRM_ACCOUNT_COLUMNS = ("crm_account_id", "account_name", "website_domain", "owner_rep_id", "created_on", "known_on")
CRM_CONTACT_COLUMNS = ("contact_id", "crm_account_id", "email", "role", "known_on")
BILLING_CUSTOMER_COLUMNS = ("billing_customer_id", "bill_to_name", "bill_to_email", "crm_account_ref", "known_on")
CONTRACT_COLUMNS = ("contract_id", "version", "crm_account_id", "signed_on", "effective_from", "term_start",
                    "term_end", "billing_frequency", "payment_terms_days", "renewal_number", "known_on")
CONTRACT_LINE_COLUMNS = ("contract_id", "version", "line_id", "product", "annual_fee_cents", "included_units",
                         "overage_cents_per_unit", "known_on")
IDENTITY_COLUMNS = ("source_system", "source_id", "true_customer_id")
OUTCOME_COLUMNS = ("contract_id", "expiry", "outcome", "acv_before", "acv_after")

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
TRUTH = {
    "identities": (IDENTITY_COLUMNS, ("source_system", "source_id")),
    "collection_behaviour": (BEHAVIOUR_COLUMNS, ("customer_id",)),
    "renewal_outcomes": (OUTCOME_COLUMNS, ("contract_id", "expiry")),
}
FIRST_YEAR_MONTHS = 12


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


def _identities(plan: Plan, billing_ids: dict[str, str]) -> list[dict]:
    rows = []
    for c in plan.customers:
        rows.append({"source_system": "crm", "source_id": c.crm_account_id, "true_customer_id": c.crm_account_id})
        rows.append({"source_system": "billing", "source_id": billing_ids[c.crm_account_id],
                     "true_customer_id": c.crm_account_id})
    return rows


def _stages(plan: Plan) -> tuple[dict[str, list[dict]], dict[str, list[dict]]]:
    """The books and the answer key rows of `plan`, by file stem."""
    v = variants(plan, rng)
    contract_rows, line_rows, outcomes = contracts(plan, rng)
    invoices, usage = billing(plan, contract_rows, line_rows, rng)
    payments, receipts, behaviour = cash(plan, invoices, v.name_for, rng)
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
    truth = {"identities": _identities(plan, v.billing_customer_ids), "collection_behaviour": behaviour,
             "renewal_outcomes": outcomes}
    return books, truth


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
    books, truth = _stages(plan)

    books_dir, truth_dir = manifest.preset_dirs(out, preset)
    for directory in (books_dir, truth_dir):
        _clear(directory)
    for stem, rows in books.items():
        columns, key = BOOKS[stem]
        _write_csv(books_dir / f"{stem}.csv", columns, key, rows)
    for stem, rows in truth.items():
        columns, key = TRUTH[stem]
        _write_csv(truth_dir / f"{stem}.csv", columns, key, rows)
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
        result[name] = manifest.drift(out, name)
    return result
