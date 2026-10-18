"""Plant one named error in a copy of the books.

`inject(books, truth, out, error, seed)` copies the books directory to `out`, edits the rows the
answer key names as that error's target (`planted_problems.json`), and writes a manifest of the
copy. Every `apply_<error>(books_dir, target, rng)` edits CSV rows in place and returns the rows it
changed; every other byte of the books stays as it was. The copy is deterministic: the draws come
from `random.Random(f"{seed}:inject:{error}")`.

The errors, and what each one does to the copy:

    duplicate_customer   a new billing customer for the target customer, under a variant of its name
                         that scores in the doubt band, with one of the customer's invoices moved to it
    wrong_period         the general-ledger entries of the target invoice move to the period before
    misapplied_payment   the target payment is re-pointed to another customer's open invoice
    unbilled_amendment   an amendment version of the target contract, its prorated invoice not issued
    defrev_gl_slip       the target 2400 recognition entry changes by an amount, 4000 side included
    orphan_bank_txn      the target receipt names an unknown payer and its payment row is removed
    rounding_slip        the target 1200 ledger line changes by one cent

Choices made where the brief is silent: the target is the answer key's row, whatever the seed (the seed
only draws the details: the invoice or payment affected, the day, the amount, the spelling); the
wrong period is the one before the invoice's, because a close reads ledger periods only up to the
month it closes, so entries moved forward from the planted month would never be seen by that close;
the manifest of the copy lists every file of the copy, under the digests of the generator and
configuration recorded beside the source books when there are any; a row that is added, edited or
removed counts as changed, named by its file's sort key.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import random
import shutil
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

from .errors import PreconditionError, UsageError
from .gen import BOOKS
from .gen.manifest import Manifest, generator_sha256, read as read_manifest, sha256_file
from .gen.plan import INJECT_ERRORS
from .gen.variants import SUFFIX_SWAPS
from .truth_reference import month_end, month_of, month_start

PLANTED_PROBLEMS = "planted_problems.json"
MANIFEST = "books.manifest.sha256"
KEYS = {stem: key for stem, (_, key) in BOOKS.items()}

UNKNOWN_PAYERS = (
    "Halcyon Ridge Partners", "Tamarack Freight Co", "Orchid Valley Trading", "Quillfeather Holdings",
    "Mossgate Logistics Ltd", "Pelican Quay Ventures",
)
AMENDMENT_BPS = (500, 3000)  # an amendment moves the annual fee up by this many bps, low to high
SLIP_DIVISORS = (10, 20, 50)  # a defrev slip is the entry's amount over one of these


class Table:
    """One books CSV, edited by row. A row nobody touches keeps its original line byte for byte."""

    def __init__(self, stem: str, path: Path, changes: dict) -> None:
        self.stem, self.path, self.changes = stem, path, changes
        text = path.read_text(encoding="utf-8")
        lines = text.split("\n")
        if lines.pop() != "" or "\r" in text:
            raise PreconditionError(f"{path.name} is not LF-terminated; inject edits files the generator wrote.")
        self.header_line = lines[0]
        self.header = next(csv.reader([lines[0]]))
        # each row: [values by column, its original line or None once edited]
        self.rows: list[list] = [[dict(zip(self.header, next(csv.reader([line])))), line] for line in lines[1:]]
        self.dirty = False

    def key_of(self, row: Mapping) -> tuple:
        return tuple(row[c] for c in KEYS[self.stem])

    def where(self, **match: str) -> list[int]:
        """Positions of the rows whose columns equal `match`."""
        return [i for i, (row, _) in enumerate(self.rows) if all(row[c] == str(v) for c, v in match.items())]

    def one(self, **match: str) -> dict:
        found = self.where(**match)
        if len(found) != 1:
            raise PreconditionError(f"{self.path.name}: expected one row with {match}, found {len(found)}; "
                                    f"inject needs the books the answer key was written for.")
        return self.rows[found[0]][0]

    def _note(self, row: Mapping, change: str) -> None:
        self.changes.setdefault((self.path.name, self.key_of(row)), change)

    def set(self, position: int, **values: str) -> None:
        row = self.rows[position][0]
        row.update({c: str(v) for c, v in values.items()})
        self.rows[position][1] = None
        self.dirty = True
        self._note(row, "edited")

    def delete(self, position: int) -> None:
        row = self.rows.pop(position)[0]
        self.dirty = True
        self.changes[(self.path.name, self.key_of(row))] = "removed"

    def insert(self, **values: str) -> None:
        """Add a row at its place in the file's sort order."""
        row = {c: str(values[c]) for c in self.header}
        key = self.key_of(row)
        position = next((i for i, (r, _) in enumerate(self.rows) if self.key_of(r) > key), len(self.rows))
        self.rows.insert(position, [row, None])
        self.dirty = True
        self._note(row, "added")

    def save(self) -> None:
        if not self.dirty:
            return
        text = self.header_line + "\n"
        for row, original in self.rows:
            if original is not None:
                text += original + "\n"
                continue
            out = io.StringIO()
            csv.writer(out, lineterminator="\n").writerow([row[c] for c in self.header])
            text += out.getvalue()
        self.path.write_text(text, encoding="utf-8", newline="")


class Books:
    """The tables of a books directory, loaded on demand, with the rows changed so far."""

    def __init__(self, directory: Path) -> None:
        self.directory = directory
        self.changes: dict[tuple[str, tuple], str] = {}
        self.tables: dict[str, Table] = {}

    def __getitem__(self, stem: str) -> Table:
        if stem not in self.tables:
            self.tables[stem] = Table(stem, self.directory / f"{stem}.csv", self.changes)
        return self.tables[stem]

    def save(self) -> list[dict]:
        for table in self.tables.values():
            table.save()
        return [{"file": file, "key": dict(zip(KEYS[file.removesuffix(".csv")], key)), "change": change}
                for (file, key), change in sorted(self.changes.items())]


def _in_month(day: str, month: str) -> bool:
    return day[:7] == month


# ------------------------------------------------------------------ the seven errors

def _variant_name(name: str, rng: random.Random) -> str:
    """The name with one letter dropped from its longest word that is not a company suffix."""
    words = name.split()
    plain = [i for i, w in enumerate(words) if w.rstrip(".").lower() not in {s.rstrip(".").lower() for s in SUFFIX_SWAPS}]
    i = max(plain or range(len(words)), key=lambda k: len(words[k]))
    word = words[i]
    cut = rng.randrange(1, len(word) - 1) if len(word) > 2 else 0
    words[i] = word[:cut] + word[cut + 1:]
    return " ".join(words)


def apply_duplicate_customer(books_dir: Path, target: Mapping, rng: random.Random) -> list[dict]:
    books = Books(books_dir)
    month, customers, invoices = target["month"], books["billing_customers"], books["invoices"]
    original = customers.one(**target["key"])["billing_customer_id"]
    account = books["crm_accounts"].one(crm_account_id=target["customer"])

    payments = {p["invoice_id"] for p, _ in books["payments"].rows}
    movable = sorted({r["invoice_id"] for r, _ in invoices.rows
                      if r["billing_customer_id"] == original and _in_month(r["invoice_date"], month)})
    if not movable:
        raise PreconditionError(f"{original} has no invoice in {month} to move to a duplicate customer.")
    unpaid = [i for i in movable if i not in payments]
    invoice_id = rng.choice(unpaid or movable)

    numbers = [int(r["billing_customer_id"].split("_")[1]) for r, _ in customers.rows]
    width = len(original.split("_")[1])
    new_id = f"{original.split('_')[0]}_{max(numbers) + 1:0{width}d}"
    customers.insert(billing_customer_id=new_id, bill_to_name=_variant_name(account["account_name"], rng),
                     bill_to_email=f"accounts.payable@{account['website_domain']}", crm_account_ref="",
                     known_on=f"{month}-01")
    for position in invoices.where(invoice_id=invoice_id):
        invoices.set(position, billing_customer_id=new_id)
    return books.save()


def apply_wrong_period(books_dir: Path, target: Mapping, rng: random.Random) -> list[dict]:
    books = Books(books_dir)
    gl = books["gl"]
    entries = gl.where(source_ref=target["key"]["invoice_id"])
    if not entries:
        raise PreconditionError(f"no ledger entry of invoice {target['key']['invoice_id']} to move.")
    for position in entries:
        gl.set(position, gl_period=month_of(month_start(gl.rows[position][0]["gl_period"]) - timedelta(days=1)))
    return books.save()


def apply_misapplied_payment(books_dir: Path, target: Mapping, rng: random.Random) -> list[dict]:
    books = Books(books_dir)
    payments, invoices = books["payments"], books["invoices"]
    payment = payments.one(**target["key"])
    account_of_contract = {r["contract_id"]: r["crm_account_id"] for r, _ in books["contracts"].rows}
    account_of_invoice: dict[str, str] = {}
    billed: dict[str, int] = {}
    for row, _ in invoices.rows:
        account_of_invoice[row["invoice_id"]] = account_of_contract[row["contract_id"]]
        billed[row["invoice_id"]] = billed.get(row["invoice_id"], 0) + int(row["amount_cents"])
    paid: dict[str, int] = {}
    for row, _ in payments.rows:
        if row["applied_on"] <= payment["applied_on"]:
            paid[row["invoice_id"]] = paid.get(row["invoice_id"], 0) + int(row["applied_cents"])
    payer = account_of_invoice[payment["invoice_id"]]
    open_invoices = sorted(
        i for i, cents in billed.items()
        if account_of_invoice[i] != payer and paid.get(i, 0) < cents
        and min(r["invoice_date"] for r, _ in invoices.rows if r["invoice_id"] == i) <= payment["applied_on"])
    if not open_invoices:
        raise PreconditionError(f"no other customer has an open invoice to re-point {payment['payment_id']} to.")
    payments.set(payments.where(**target["key"])[0], invoice_id=rng.choice(open_invoices))
    return books.save()


def apply_unbilled_amendment(books_dir: Path, target: Mapping, rng: random.Random) -> list[dict]:
    books = Books(books_dir)
    contracts, lines = books["contracts"], books["contract_lines"]
    contract_id, month = target["key"]["contract_id"], target["month"]
    latest = max((r for r, _ in contracts.rows if r["contract_id"] == contract_id), key=lambda r: int(r["version"]))
    first = max(month_start(month), date.fromisoformat(latest["term_start"]) + timedelta(days=1))
    last = min(month_end(month), date.fromisoformat(latest["term_end"]))
    if first > last:
        raise PreconditionError(f"{contract_id} has no day in {month} inside its term to amend on.")
    effective = (first + timedelta(days=rng.randrange((last - first).days + 1))).isoformat()
    version = str(int(latest["version"]) + 1)
    contracts.insert(**{**latest, "version": version, "signed_on": effective, "effective_from": effective,
                        "known_on": effective})
    bps = rng.randint(*AMENDMENT_BPS)
    for row, _ in [r for r in lines.rows if r[0]["contract_id"] == contract_id and r[0]["version"] == latest["version"]]:
        fee = int(row["annual_fee_cents"])
        if row["product"] == "subscription":
            fee += fee * bps // 10000
        lines.insert(**{**row, "version": version, "annual_fee_cents": fee, "known_on": effective})
    return books.save()


def apply_defrev_gl_slip(books_dir: Path, target: Mapping, rng: random.Random) -> list[dict]:
    books = Books(books_dir)
    gl = books["gl"]
    entry_id, line_no = target["key"]["entry_id"], target["key"]["line_no"]
    position = gl.where(entry_id=entry_id, line_no=line_no)[0]
    others = [p for p in gl.where(entry_id=entry_id) if p != position]
    if len(others) != 1:
        raise PreconditionError(f"entry {entry_id} is not a two-line recognition entry.")
    amount = int(gl.rows[position][0]["amount_cents"])
    slip = max(1, abs(amount) // rng.choice(SLIP_DIVISORS))
    gl.set(position, amount_cents=amount + slip)
    gl.set(others[0], amount_cents=int(gl.rows[others[0]][0]["amount_cents"]) - slip)
    return books.save()


def apply_orphan_bank_txn(books_dir: Path, target: Mapping, rng: random.Random) -> list[dict]:
    books = Books(books_dir)
    bank, payments = books["bank_transactions"], books["payments"]
    bank_txn_id = target["key"]["bank_txn_id"]
    bank.one(bank_txn_id=bank_txn_id)
    bank.set(bank.where(bank_txn_id=bank_txn_id)[0], counterparty_text=rng.choice(UNKNOWN_PAYERS),
             reference_text="wire received")
    for position in reversed(payments.where(bank_txn_id=bank_txn_id)):
        payments.delete(position)
    return books.save()


def apply_rounding_slip(books_dir: Path, target: Mapping, rng: random.Random) -> list[dict]:
    books = Books(books_dir)
    gl = books["gl"]
    position = gl.where(**target["key"])[0]
    gl.set(position, amount_cents=int(gl.rows[position][0]["amount_cents"]) + 1)
    return books.save()


APPLY: dict[str, Callable[[Path, Mapping, random.Random], list[dict]]] = {
    "duplicate_customer": apply_duplicate_customer,
    "wrong_period": apply_wrong_period,
    "misapplied_payment": apply_misapplied_payment,
    "unbilled_amendment": apply_unbilled_amendment,
    "defrev_gl_slip": apply_defrev_gl_slip,
    "orphan_bank_txn": apply_orphan_bank_txn,
    "rounding_slip": apply_rounding_slip,
}
assert tuple(APPLY) == INJECT_ERRORS


# ------------------------------------------------------------------ the command's work

@dataclass(frozen=True)
class Injected:
    error: str
    month: str
    changed: list[dict]
    manifest: Path


def target_of(truth: Path, error: str) -> tuple[dict, dict]:
    """The answer key's row for `error`, and the whole key."""
    path = truth / PLANTED_PROBLEMS
    if not path.is_file():
        raise PreconditionError(f"no {PLANTED_PROBLEMS} at {truth}: inject needs the answer key written beside the "
                                f"books; run c2c gen.")
    planted = json.loads(path.read_text(encoding="utf-8"))
    if error not in planted["errors"]:
        raise PreconditionError(f"{path} names no target for {error}: the books were generated without an injection month.")
    return planted["errors"][error], planted


def write_manifest(books: Path, out: Path, preset: str, seed: int) -> Path:
    """The copy's manifest: a digest per file, under the source's generator and configuration digests."""
    source = books.parent / MANIFEST
    if source.is_file():
        recorded = read_manifest(source)
        generator, config = recorded.generator_sha256, recorded.config_sha256
    else:
        generator, config = generator_sha256(), hashlib.sha256(b"").hexdigest()
    files = {p.name: sha256_file(p) for p in sorted(out.iterdir()) if p.is_file() and p.name != MANIFEST}
    path = out / MANIFEST
    path.write_text(Manifest(seed, preset, generator, config, files).render(), encoding="utf-8")
    return path


def inject(books: Path, truth: Path, out: Path, error: str, seed: int) -> Injected:
    if not (books / "window.json").is_file():
        raise PreconditionError(f"no books at {books}: window.json is missing; generate them first.")
    target, planted = target_of(truth, error)
    if out.exists() and any(out.iterdir()):
        raise UsageError(f"{out} is not empty: inject writes a fresh copy of the books there.")
    out.mkdir(parents=True, exist_ok=True)
    for path in sorted(books.iterdir()):
        if path.is_file() and path.name != MANIFEST:
            shutil.copyfile(path, out / path.name)
    changed = APPLY[error](out, target, random.Random(f"{seed}:inject:{error}"))
    return Injected(error, target["month"], changed, write_manifest(books, out, planted["preset"], seed))
