"""Payments, bank receipts and collection behaviour from the mini plan's invoices."""

from __future__ import annotations

from collections import Counter, defaultdict
from datetime import date

import pytest

from c2c import truth_reference as tr
from c2c.gen.billing import billing
from c2c.gen.cash import (BANK_COLUMNS, BEHAVIOUR_COLUMNS, OPENING_CASH_CENTS, PAYMENT_COLUMNS, bank_row, cash,
                          next_business_day, previous_business_day)
from c2c.gen.contracts import contracts
from c2c.gen.plan import build_plan, load_config
from c2c.gen.seeds import rng
from c2c.gen.variants import variants


@pytest.fixture(scope="module")
def plan():
    cfg = load_config()
    return build_plan("mini", cfg, cfg["seed"])


@pytest.fixture(scope="module")
def invoices(plan):
    contract_rows, line_rows, _ = contracts(plan, rng)
    return billing(plan, contract_rows, line_rows, rng)[0]


@pytest.fixture(scope="module")
def books(plan, invoices):
    return cash(plan, invoices, variants(plan, rng).name_for, rng)


def totals(invoices):
    out = defaultdict(int)
    for r in invoices:
        out[r["invoice_id"]] += r["amount_cents"]
    return out


def test_every_payment_has_a_bank_row_and_the_sums_match(books):
    payments, bank, _ = books
    by_txn = defaultdict(int)
    for p in payments:
        by_txn[p["bank_txn_id"]] += p["applied_cents"]
    receipts = {b["bank_txn_id"]: b for b in bank[1:]}
    assert set(by_txn) == set(receipts)
    for txn, total in by_txn.items():
        assert receipts[txn]["amount_cents"] == total > 0
    assert len({p["payment_id"] for p in payments}) == len(payments)
    assert len({b["bank_txn_id"] for b in bank}) == len(bank)


def test_rows_have_the_file_columns_and_are_known_on_the_day_they_post(books):
    payments, bank, behaviour = books
    assert all(tuple(p) == PAYMENT_COLUMNS and p["known_on"] == p["applied_on"] for p in payments)
    assert all(tuple(b) == BANK_COLUMNS and b["known_on"] == b["posted_on"] for b in bank)
    assert all(tuple(b) == BEHAVIOUR_COLUMNS for b in behaviour)
    assert all(isinstance(p["applied_cents"], int) for p in payments)
    assert all(isinstance(b["amount_cents"], int) for b in bank)


def test_bank_rows_post_on_business_days_inside_the_books(plan, books):
    _, bank, _ = books
    calendar = set(plan.calendar)
    assert all(b["posted_on"] in calendar for b in bank)
    assert max(b["posted_on"] for b in bank) <= tr.month_end(plan.last_month).isoformat()
    assert [b["posted_on"] for b in bank] == sorted(b["posted_on"] for b in bank)


def test_the_first_bank_row_is_the_opening_balance(plan, books):
    _, bank, _ = books
    assert bank[0]["amount_cents"] == OPENING_CASH_CENTS and bank[0]["bank_txn_id"] == "bk_000001"
    assert bank[0]["posted_on"] == next_business_day(plan, date.fromisoformat(plan.first_month + "-01")).isoformat()
    assert all(b["amount_cents"] > 0 for b in bank)


def test_a_receipt_names_its_customers_bank_spelling_and_its_invoices(plan, invoices, books):
    payments, bank, _ = books
    name_for = variants(plan, rng).name_for
    customer_of = {bc: a for a, bc in variants(plan, rng).billing_customer_ids.items()}
    invoice_ids = defaultdict(list)
    for p in payments:
        invoice_ids[p["bank_txn_id"]].append(p["invoice_id"])
    customers = {p["bank_txn_id"]: customer_of[p["billing_customer_id"]] for p in payments}
    for b in bank[1:]:
        assert b["counterparty_text"] == name_for(customers[b["bank_txn_id"]], "bank")
        assert all(i in b["reference_text"] for i in invoice_ids[b["bank_txn_id"]])
    bare = [b for b in bank[1:] if b["reference_text"].replace(", ", "").replace("inv_", "").isdigit()]
    assert bare and len(bare) < len(bank) - 1, "some references are bare invoice ids, some are not"


def test_payments_apply_to_known_invoices_of_the_right_customer(invoices, books):
    payments, _, _ = books
    owner = {r["invoice_id"]: r["billing_customer_id"] for r in invoices}
    date_of = {r["invoice_id"]: r["invoice_date"] for r in invoices}
    for p in payments:
        assert owner[p["invoice_id"]] == p["billing_customer_id"]
        assert p["applied_on"] > date_of[p["invoice_id"]]


def test_the_planted_payment_shapes_appear_exactly_once(plan, invoices, books):
    payments, bank, _ = books
    total = totals(invoices)
    applied = defaultdict(list)
    for p in payments:
        applied[p["invoice_id"]].append(p)
    partial = [i for i, ps in applied.items() if len(ps) == 2]
    over = [i for i, ps in applied.items() if sum(p["applied_cents"] for p in ps) > total[i]]
    per_txn = Counter(p["bank_txn_id"] for p in payments)
    multi = [t for t, n in per_txn.items() if n > 1]
    assert len(partial) == 1 and len(over) == 1 and len(multi) == 1

    owner = {r["invoice_id"]: r["billing_customer_id"] for r in invoices}
    ids = variants(plan, rng).billing_customer_ids
    assert owner[partial[0]] == ids[plan.roles["partial_payment"][0]]
    assert owner[over[0]] == ids[plan.roles["overpayment"][0]]
    pair = [p for p in payments if p["bank_txn_id"] == multi[0]]
    assert {owner[p["invoice_id"]] for p in pair} == {ids[plan.roles["multi_payment"][0]]}
    assert all(p["applied_cents"] == total[p["invoice_id"]] for p in pair)

    first, second = sorted(applied[partial[0]], key=lambda p: p["applied_on"])
    assert first["applied_cents"] + second["applied_cents"] == total[partial[0]]
    assert 0 < first["applied_cents"] < total[partial[0]] and first["applied_on"] < second["applied_on"]
    assert sum(p["applied_cents"] for p in applied[over[0]]) > total[over[0]]


def test_every_other_invoice_is_paid_in_full_or_still_open(plan, invoices, books):
    payments, _, _ = books
    total = totals(invoices)
    shaped = {a for role in ("partial_payment", "overpayment", "multi_payment") for a in plan.roles[role]}
    ids = variants(plan, rng).billing_customer_ids
    shaped_bc = {ids[a] for a in shaped}
    applied = defaultdict(int)
    for p in payments:
        applied[p["invoice_id"]] += p["applied_cents"]
    owner = {r["invoice_id"]: r["billing_customer_id"] for r in invoices}
    for inv, cents in total.items():
        if owner[inv] in shaped_bc:
            continue
        assert applied.get(inv, 0) in (0, cents) if cents > 0 else inv not in applied
    assert sum(1 for i, c in total.items() if c > 0 and applied.get(i, 0) == c) > len(total) // 2


def test_days_to_pay_follow_each_customers_behaviour(plan, invoices, books):
    payments, _, behaviour = books
    ids = variants(plan, rng).billing_customer_ids
    by_acct = {c.crm_account_id: c.dtp for c in plan.customers}
    date_of = {r["invoice_id"]: date.fromisoformat(r["invoice_date"]) for r in invoices}
    customer_of = {bc: a for a, bc in ids.items()}
    for p in payments:
        d = by_acct[customer_of[p["billing_customer_id"]]]
        days = (date.fromisoformat(p["applied_on"]) - date_of[p["invoice_id"]]).days
        # weekend shift (up to 3 days), and a partial remainder or a pooled multi receipt run later
        assert days >= max(d.median_days - d.spread_days, 1)
    assert [b["customer_id"] for b in behaviour] == [c.crm_account_id for c in plan.customers]
    assert all(b["median_days"] == by_acct[b["customer_id"]].median_days for b in behaviour)


def test_cash_is_deterministic_and_seeded(plan, invoices, books):
    again = cash(plan, invoices, variants(plan, rng).name_for, rng)
    assert again == books
    cfg = load_config()
    other = build_plan("mini", cfg, cfg["seed"] + 1)
    contract_rows, line_rows, _ = contracts(other, rng)
    other_invoices = billing(other, contract_rows, line_rows, rng)[0]
    assert cash(other, other_invoices, variants(other, rng).name_for, rng)[0] != books[0]


def test_the_cash_rollforward_of_the_bank_rows_chains(books):
    _, bank, _ = books
    rolls = tr.cash_rollforward(bank)
    receipts = sum(r.additions for r in rolls.values())
    assert receipts == sum(b["amount_cents"] for b in bank[1:])
    assert list(rolls.values())[-1].closing == OPENING_CASH_CENTS + receipts


def test_credit_invoices_are_not_paid(plan, invoices):
    credit = dict(invoices[0], invoice_id="inv_900001", line_type="credit", amount_cents=-500, invoice_date="2025-02-03")
    payments, _, _ = cash(plan, [*invoices, credit], variants(plan, rng).name_for, rng)
    assert "inv_900001" not in {p["invoice_id"] for p in payments}


def test_an_unknown_billing_customer_is_an_error(plan, invoices):
    with pytest.raises(ValueError, match="bill_9999"):
        cash(plan, [*invoices, dict(invoices[0], invoice_id="inv_900002", billing_customer_id="bill_9999")], variants(plan, rng).name_for, rng)


def test_business_day_helpers_move_in_both_directions(plan):
    saturday, monday, friday = date(2024, 10, 5), date(2024, 10, 7), date(2024, 10, 4)
    assert next_business_day(plan, saturday) == monday and next_business_day(plan, monday) == monday
    assert previous_business_day(plan, saturday) == friday and previous_business_day(plan, friday) == friday
    assert next_business_day(plan, date(2030, 1, 1)) is None
    assert previous_business_day(plan, date(2000, 1, 1)) is None


def test_bank_row_builds_a_row_known_on_the_posting_day():
    row = bank_row("bk_000099", date(2025, 3, 14), -125_000, "Cirrus Compute (synthetic)", "bill 12")
    assert tuple(row) == BANK_COLUMNS
    assert row["posted_on"] == row["known_on"] == "2025-03-14" and row["amount_cents"] == -125_000
