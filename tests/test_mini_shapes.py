"""The committed `mini` preset: its books carry every planted shape, its answer key looks past
the books' window, and its expected outputs equal a recomputation from the books."""

from __future__ import annotations

import csv
import importlib.util
import json
import shutil
from collections import defaultdict
from datetime import date, timedelta

import pytest

from c2c import gen
from c2c.gen import BOOKS, manifest
from c2c.gen.plan import INJECT_ERRORS

from conftest import MINI_BOOKS, MINI_TRUTH, MINI_WINDOW, ROOT

FIXTURES = ROOT / "tests" / "fixtures"
EXPECTED = FIXTURES / "expected"
OPEN_STAGES = {"S1", "S2", "S3", "S4", "S5"}


def read(path):
    with path.open(encoding="utf-8", newline="") as fh:
        return list(csv.DictReader(fh))


def books(stem):
    return read(MINI_BOOKS / f"{stem}.csv")


def truth(stem):
    return read(MINI_TRUTH / f"{stem}.csv")


@pytest.fixture(scope="module")
def write_expected():
    spec = importlib.util.spec_from_file_location("write_expected", ROOT / "scripts" / "write-expected.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def invoice_totals():
    totals = defaultdict(int)
    for row in books("invoices"):
        totals[row["invoice_id"]] += int(row["amount_cents"])
    return totals


def test_the_committed_files_match_their_manifest():
    assert gen.verify(FIXTURES, "mini") == {"mini": []}
    header = manifest.read(manifest.manifest_path(FIXTURES, "mini"))
    assert header.preset == "mini"
    assert {p.name for p in MINI_BOOKS.iterdir()} == {f"{stem}.csv" for stem in BOOKS} | {"window.json"}


def test_the_books_cover_the_window_and_the_truth_looks_past_it():
    first, last = MINI_WINDOW
    assert json.loads((MINI_BOOKS / "window.json").read_text(encoding="utf-8")) == {
        "first_month": first, "last_month": last}
    in_window = {row["invoice_date"][:7] for row in books("invoices")} | {
        row["posted_on"][:7] for row in books("bank_transactions")}
    assert first <= min(in_window) and max(in_window) == last
    assert len(books("crm_accounts")) == 10

    future_invoices = truth("future_invoices")
    assert future_invoices and {row["invoice_date"][:7] for row in future_invoices} <= {"2026-04", "2026-05", "2026-06"}
    days = [row["date"] for row in truth("future_cash")]
    assert (days[0], days[-1]) == ("2026-04-01", "2026-06-30") and len(days) == 91


def test_one_customer_has_four_name_variants():
    names = defaultdict(set)
    for row in truth("identities"):
        if row["source_system"].endswith("_name"):
            names[row["true_customer_id"]].add(row["source_id"])
    counts = sorted(len(spellings) for spellings in names.values())
    assert counts[-1] == 4 and counts[-2] < 4 and len(names) == 10


def test_the_doubt_band_has_one_pair_of_each_decision():
    pairs = {row["decision"]: row for row in truth("matches_expected")}
    assert sorted(pairs) == ["different", "same"] and len(truth("matches_expected")) == 2
    accounts = {row["crm_account_id"] for row in books("crm_accounts")}
    billing = {row["billing_customer_id"] for row in books("billing_customers")}
    for row in pairs.values():
        assert row["source_a"] in accounts and row["source_b"] in billing


def test_a_contract_is_amended_mid_term():
    amended = [row for row in books("contracts") if int(row["version"]) > 1]
    assert len(amended) == 1
    row = amended[0]
    assert row["term_start"] < row["effective_from"] <= row["term_end"]
    assert row["signed_on"] <= row["effective_from"] and row["known_on"] == row["signed_on"]
    lines = [r for r in books("contract_lines") if (r["contract_id"], r["version"]) == (row["contract_id"], row["version"])]
    assert lines


def test_an_invoice_is_paid_in_two_parts():
    paid = defaultdict(list)
    for row in books("payments"):
        paid[row["invoice_id"]].append(row)
    totals = invoice_totals()
    split = [inv for inv, rows in paid.items() if len({r["bank_txn_id"] for r in rows}) > 1]
    assert len(split) == 1
    rows = sorted(paid[split[0]], key=lambda r: r["applied_on"])
    assert 0 < int(rows[0]["applied_cents"]) < totals[split[0]]
    assert sum(int(r["applied_cents"]) for r in rows) == totals[split[0]]


def test_one_receipt_exceeds_its_invoice():
    totals = invoice_totals()
    applied = defaultdict(int)
    for row in books("payments"):
        applied[row["invoice_id"]] += int(row["applied_cents"])
    over = [inv for inv, cents in applied.items() if cents > totals[inv]]
    assert len(over) == 1


def test_one_receipt_pays_several_invoices():
    bank = {row["bank_txn_id"]: int(row["amount_cents"]) for row in books("bank_transactions")}
    by_receipt = defaultdict(list)
    for row in books("payments"):
        by_receipt[row["bank_txn_id"]].append(row)
    several = [txn for txn, rows in by_receipt.items() if len({r["invoice_id"] for r in rows}) > 1]
    assert len(several) == 1
    assert bank[several[0]] == sum(int(r["applied_cents"]) for r in by_receipt[several[0]])


def test_one_open_opportunity_is_past_its_expected_close():
    latest = {}
    for row in books("pipeline_snapshots"):
        latest[row["opp_id"]] = max(latest.get(row["opp_id"], row), row, key=lambda r: r["snapshot_date"])
    last_day = max(row["snapshot_date"] for row in latest.values())
    assert last_day[:7] == MINI_WINDOW[1]
    stale = [row for row in latest.values()
             if row["stage"] in OPEN_STAGES and row["snapshot_date"] == last_day
             and row["expected_close_date"] < last_day]
    assert len(stale) == 1
    assert date.fromisoformat(last_day) - date.fromisoformat(stale[0]["expected_close_date"]) <= timedelta(days=60)


def test_every_injected_error_has_a_target_row():
    planted = json.loads((MINI_TRUTH / "planted_problems.json").read_text(encoding="utf-8"))
    assert planted["preset"] == "mini" and planted["inject_month"] == "2026-01"
    assert sorted(planted["errors"]) == sorted(INJECT_ERRORS)
    for error, target in planted["errors"].items():
        rows = [row for row in books(target["table"])
                if all(row[column] == str(value) for column, value in target["key"].items())]
        assert len(rows) == 1, error
        assert target["month"] == planted["inject_month"], error


def test_the_expected_outputs_equal_a_recomputation(write_expected):
    recomputed = write_expected.expected(MINI_BOOKS, MINI_TRUTH)
    assert set(recomputed) == {"rollforward.csv", "bridge.csv"}
    for name, text in recomputed.items():
        assert (EXPECTED / name).read_text(encoding="utf-8") == text, name


def test_the_expected_rollforward_agrees_with_the_answer_key():
    key = {(r["customer_id"], r["month"]): r for r in truth("expected_rollforward")}
    expected = read(EXPECTED / "rollforward.csv")
    assert len(expected) == len(key) == 10 * 17
    for row in expected:
        assert {c: v for c, v in key[row["customer_id"], row["month"]].items() if c in row} == row


def test_the_expected_bridge_conserves_and_ties_to_the_bank():
    receipts = defaultdict(int)
    for row in books("bank_transactions")[1:]:
        if int(row["amount_cents"]) > 0:
            receipts[row["posted_on"][:7]] += int(row["amount_cents"])
    rows = read(EXPECTED / "bridge.csv")
    assert [r["month"] for r in rows][-1] == MINI_WINDOW[1]
    for row in rows:
        revenue, defrev, billings, ar, cash = (int(row[c]) for c in
                                               ("revenue", "defrev_movement", "net_billings", "ar_movement", "receipts"))
        assert revenue + defrev == billings and billings - ar == cash
        assert cash == receipts[row["month"]], row["month"]


def test_drift_in_a_copy_of_the_books_is_caught(mini_books_copy):
    out = mini_books_copy.parent
    shutil.copytree(MINI_TRUTH, out / "truth")
    shutil.copy(FIXTURES / "books.manifest.sha256", out)
    assert gen.verify(out, "mini") == {"mini": []}
    target = mini_books_copy / "invoices.csv"
    target.write_bytes(target.read_bytes() + b"x\n")
    assert gen.verify(out, "mini") == {"mini": ["books/invoices.csv"]}
