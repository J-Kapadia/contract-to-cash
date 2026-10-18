"""`c2c inject`: each named error changes exactly the rows it reports, in the planted month."""

from __future__ import annotations

import csv
import hashlib
import io
import json

import pytest

from c2c.gen import BOOKS
from c2c.gen.manifest import read as read_manifest
from c2c.gen.plan import INJECT_ERRORS
from c2c.load import MONTH_COLUMN

from conftest import MINI_BOOKS, MINI_TRUTH, c2c

MONTH = "2026-01"
MANIFEST = "books.manifest.sha256"


def rows(path):
    with path.open(encoding="utf-8", newline="") as fh:
        return list(csv.DictReader(fh))


def by_key(directory, stem):
    key = BOOKS[stem][1]
    return {tuple(r[c] for c in key): r for r in rows(directory / f"{stem}.csv")}


def tree(root):
    return {p.name: p.read_bytes() for p in sorted(root.iterdir()) if p.is_file()}


def planted():
    return json.loads((MINI_TRUTH / "planted_problems.json").read_text(encoding="utf-8"))


def inject(error, out, *extra):
    result = c2c("inject", "--error", error, "--books", str(MINI_BOOKS), "--out", str(out), "--json", *extra)
    assert result.code == 0, result.stderr
    return result.json


def changed_rows(directory):
    """Every (file, key) whose row differs between the clean books and `directory`, with both versions."""
    found = {}
    for stem in BOOKS:
        clean, copy = by_key(MINI_BOOKS, stem), by_key(directory, stem)
        for key in clean.keys() | copy.keys():
            if clean.get(key) != copy.get(key):
                found[(f"{stem}.csv", key)] = (clean.get(key), copy.get(key))
    return found


@pytest.fixture(scope="module", params=INJECT_ERRORS)
def injected(request, tmp_path_factory):
    out = tmp_path_factory.mktemp(request.param) / "copy"
    return request.param, out, inject(request.param, out)


def test_the_copy_differs_in_exactly_the_rows_the_envelope_names(injected):
    error, out, envelope = injected
    named = {(c["file"], tuple(str(c["key"][col]) for col in BOOKS[c["file"].removesuffix(".csv")][1]))
             for c in envelope["changed"]}
    assert named and named == set(changed_rows(out))
    assert envelope["error"] == error and envelope["month"] == MONTH


def test_every_other_byte_is_identical_and_the_copy_has_its_own_manifest(injected):
    _, out, envelope = injected
    clean, copy = tree(MINI_BOOKS), tree(out)
    touched = {c["file"] for c in envelope["changed"]}
    assert copy.keys() == clean.keys() | {MANIFEST}
    assert {name for name in clean if clean[name] != copy[name]} == touched
    for name in touched:  # a changed file differs only in the lines of the changed rows
        old, new = clean[name].decode().splitlines(), copy[name].decode().splitlines()
        assert old[0] == new[0]
        assert len(set(old) ^ set(new)) <= 2 * sum(c["file"] == name for c in envelope["changed"])
    recorded = read_manifest(out / MANIFEST)
    assert recorded.files == {n: hashlib.sha256(b).hexdigest() for n, b in copy.items() if n != MANIFEST}


def test_the_changed_rows_are_in_the_planted_month(injected):
    error, out, _ = injected
    assert planted()["inject_month"] == MONTH
    for (file, _), (before, after) in changed_rows(out).items():
        stem = file.removesuffix(".csv")
        row = before or after
        assert row[MONTH_COLUMN[stem]][:7] == MONTH, (file, row)


def test_two_runs_give_the_same_copy(injected, tmp_path):
    error, out, _ = injected
    inject(error, tmp_path / "again")
    assert tree(tmp_path / "again") == tree(out)


def test_the_seed_is_part_of_the_draw(tmp_path):
    inject("unbilled_amendment", tmp_path / "a", "--seed", "1")
    inject("unbilled_amendment", tmp_path / "b", "--seed", "1")
    inject("unbilled_amendment", tmp_path / "c", "--seed", "2")
    assert tree(tmp_path / "a") == tree(tmp_path / "b") != tree(tmp_path / "c")


# ------------------------------------------------------------------ what each error does


def test_duplicate_customer_adds_a_variant_billing_customer_with_an_invoice(tmp_path):
    inject("duplicate_customer", tmp_path / "o")
    clean = by_key(MINI_BOOKS, "billing_customers")
    added = [r for k, r in by_key(tmp_path / "o", "billing_customers").items() if k not in clean]
    account = next(r for r in rows(MINI_BOOKS / "crm_accounts.csv") if r["crm_account_id"] == planted()["errors"]["duplicate_customer"]["customer"])
    assert len(added) == 1 and added[0]["crm_account_ref"] == ""
    assert added[0]["bill_to_name"] != account["account_name"]
    assert added[0]["bill_to_email"].endswith("@" + account["website_domain"])
    assert [r for r in rows(tmp_path / "o" / "invoices.csv") if r["billing_customer_id"] == added[0]["billing_customer_id"]]


def test_wrong_period_moves_the_invoice_entries_to_the_previous_period(tmp_path):
    inject("wrong_period", tmp_path / "o")
    invoice = planted()["errors"]["wrong_period"]["key"]["invoice_id"]
    for (_, _), (before, after) in changed_rows(tmp_path / "o").items():
        assert before["source_ref"] == invoice and before["gl_period"] == MONTH
        assert after == {**before, "gl_period": "2025-12"}


def test_misapplied_payment_points_to_another_customers_open_invoice(tmp_path):
    inject("misapplied_payment", tmp_path / "o")
    [(before, after)] = changed_rows(tmp_path / "o").values()
    assert after == {**before, "invoice_id": after["invoice_id"]} and after["invoice_id"] != before["invoice_id"]
    contract_of = {r["invoice_id"]: r["contract_id"] for r in rows(MINI_BOOKS / "invoices.csv")}
    account_of = {r["contract_id"]: r["crm_account_id"] for r in rows(MINI_BOOKS / "contracts.csv")}
    assert account_of[contract_of[after["invoice_id"]]] != account_of[contract_of[before["invoice_id"]]]


def test_unbilled_amendment_adds_a_version_inside_the_month_and_no_invoice(tmp_path):
    inject("unbilled_amendment", tmp_path / "o")
    contracts, lines = by_key(tmp_path / "o", "contracts"), by_key(tmp_path / "o", "contract_lines")
    new = contracts[("ctr_0010_00", "2")]
    assert new["effective_from"].startswith(MONTH) and new["signed_on"] == new["known_on"] == new["effective_from"]
    fee = lambda version: lines[("ctr_0010_00", version, "ctr_0010_00_L1")]["annual_fee_cents"]
    assert int(fee("2")) > int(fee("1"))
    assert rows(tmp_path / "o" / "invoices.csv") == rows(MINI_BOOKS / "invoices.csv")


def test_defrev_gl_slip_changes_both_sides_of_the_entry_and_keeps_it_balanced(tmp_path):
    inject("defrev_gl_slip", tmp_path / "o")
    changes = list(changed_rows(tmp_path / "o").values())
    assert sorted(b["gl_account"] for b, _ in changes) == ["2400", "4000"]
    assert all(a["amount_cents"] != b["amount_cents"] for b, a in changes)
    assert sum(int(a["amount_cents"]) - int(b["amount_cents"]) for b, a in changes) == 0


def test_orphan_bank_txn_names_an_unknown_payer_and_loses_its_payment(tmp_path):
    inject("orphan_bank_txn", tmp_path / "o")
    key = planted()["errors"]["orphan_bank_txn"]["key"]["bank_txn_id"]
    bank = by_key(tmp_path / "o", "bank_transactions")[(key,)]
    known = {r["bill_to_name"] for r in rows(MINI_BOOKS / "billing_customers.csv")} | {r["account_name"] for r in rows(MINI_BOOKS / "crm_accounts.csv")}
    assert bank["counterparty_text"] not in known and "inv_" not in bank["reference_text"]
    assert not [r for r in rows(tmp_path / "o" / "payments.csv") if r["bank_txn_id"] == key]


def test_rounding_slip_adds_one_cent_to_one_receivable_line(tmp_path):
    inject("rounding_slip", tmp_path / "o")
    [(before, after)] = changed_rows(tmp_path / "o").values()
    assert before["gl_account"] == "1200" and int(after["amount_cents"]) == int(before["amount_cents"]) + 1


# ------------------------------------------------------------------ invocation


def test_the_mini_fixture_names_the_books(tmp_path):
    result = c2c("inject", "--error", "rounding_slip", "--fixture", "mini", "--out", str(tmp_path / "o"), "--json",
                 env={"C2C_BOOKS": ""})
    assert result.code == 0, result.stderr
    assert result.json["changed"][0]["file"] == "gl.csv"


def test_an_unknown_error_exits_2(tmp_path):
    result = c2c("inject", "--error", "typo_slip", "--books", str(MINI_BOOKS), "--out", str(tmp_path / "o"), "--json")
    assert result.code == 2 and result.json["status"] == "failed"
    assert not (tmp_path / "o").exists()


def test_no_error_exits_2(tmp_path):
    assert c2c("inject", "--books", str(MINI_BOOKS), "--out", str(tmp_path / "o")).code == 2


def test_all_is_not_available_yet(tmp_path):
    result = c2c("inject", "--all", "--books", str(MINI_BOOKS), "--out", str(tmp_path / "o"), "--json")
    assert result.code == 2 and "not available" in result.json["message"]


def test_a_non_empty_out_is_refused_and_left_alone(tmp_path):
    (tmp_path / "o").mkdir()
    (tmp_path / "o" / "keep.txt").write_text("keep")
    result = c2c("inject", "--error", "rounding_slip", "--books", str(MINI_BOOKS), "--out", str(tmp_path / "o"), "--json")
    assert result.code == 2 and [p.name for p in (tmp_path / "o").iterdir()] == ["keep.txt"]


def test_books_without_an_answer_key_exit_4(mini_books_copy, tmp_path):
    result = c2c("inject", "--error", "rounding_slip", "--books", str(mini_books_copy), "--out", str(tmp_path / "o"), "--json",
                 env={"C2C_TRUTH": str(tmp_path / "no-truth")})
    assert result.code == 4 and "planted_problems.json" in result.json["message"]
