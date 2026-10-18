"""Operating costs and the general ledger: balanced entries, tie-outs to the roll-forwards,
business-day payroll, vendor bills and the bank rows that pay them."""

from __future__ import annotations

from collections import defaultdict
from datetime import date, timedelta

import pytest

from c2c import truth_reference as tr
from c2c.gen.gl import GL_COLUMNS, gl, subledger_names
from c2c.gen.opex import (BANK_COLUMNS, BILL_COLUMNS, COMMITMENT_COLUMNS, PAYROLL_COLUMNS, Opex, opex)
from c2c.gen.plan import build_plan, load_config
from c2c.gen.seeds import rng
from c2c.gen.variants import variants


@pytest.fixture(scope="module")
def plan():
    cfg = load_config()
    return build_plan("mini", cfg, cfg["seed"])


@pytest.fixture(scope="module")
def costs(plan):
    return opex(plan, [], [], rng)


def inv(invoice_id, line_no, customer, day, start, end, kind, cents):
    return dict(invoice_id=invoice_id, line_no=line_no, billing_customer_id=customer, contract_id="c1",
                contract_line_id="L1", invoice_date=day, gl_period=day[:7], due_date=day, service_start=start,
                service_end=end, line_type=kind, amount_cents=cents)


def pay(payment_id, customer, invoice, cents, day, txn):
    return dict(payment_id=payment_id, bank_txn_id=txn, billing_customer_id=customer, invoice_id=invoice,
                applied_cents=cents, applied_on=day)


OPENING = dict(bank_txn_id="bk_0001", posted_on="2024-10-01", amount_cents=900_000_000,
               counterparty_text="OPENING BALANCE", reference_text="opening balance", known_on="2024-10-01")

INVOICES = [
    inv("inv_1", 1, "bc_a", "2025-01-01", "2025-01-01", "2025-12-31", "subscription", 1_200_001),
    inv("inv_1", 2, "bc_a", "2025-01-01", "2025-01-01", "2025-12-31", "subscription", 333_333),
    inv("inv_2", 1, "bc_b", "2025-01-01", "2025-01-01", "2025-03-31", "subscription", 500_003),
    inv("inv_3", 1, "bc_b", "2025-02-28", "2025-02-01", "2025-02-28", "overage", 75_000),
    inv("inv_4", 1, "bc_a", "2025-03-15", "2025-03-15", "2025-12-31", "credit", -250_001),
    inv("inv_5", 1, "bc_b", "2025-04-01", "2025-04-01", "2025-06-30", "subscription", 500_003),
]
PAYMENTS = [
    pay("pay_1", "bc_a", "inv_1", 1_000_000, "2025-02-03", "bk_0002"),
    pay("pay_2", "bc_b", "inv_2", 500_003, "2025-02-10", "bk_0003"),
    pay("pay_3", "bc_a", "inv_1", 233_333, "2025-04-07", "bk_0004"),
    pay("pay_4", "bc_b", "inv_3", 75_000, "2025-04-08", "bk_0005"),
]


def bank_with(costs):
    receipts = [dict(bank_txn_id=p["bank_txn_id"], posted_on=p["applied_on"], amount_cents=p["applied_cents"],
                     counterparty_text="X", reference_text=p["invoice_id"], known_on=p["applied_on"])
                for p in PAYMENTS]
    return sorted([OPENING, *receipts, *costs.bank_rows], key=lambda r: (r["posted_on"], r["bank_txn_id"]))


def balances(rows, account, month):
    """The ledger balance of `account` at the end of `month`, debit positive."""
    return sum(r["amount_cents"] for r in rows if r["gl_account"] == account and r["gl_period"] <= month)


@pytest.fixture(scope="module")
def ledger(plan, costs):
    return gl(INVOICES, PAYMENTS, bank_with(costs), costs, plan.months)


# -- the ledger ---------------------------------------------------------------------------------

def test_every_entry_balances(ledger):
    totals = defaultdict(int)
    for r in ledger:
        totals[r["entry_id"]] += r["amount_cents"]
    assert totals and all(t == 0 for t in totals.values())
    assert sum(r["amount_cents"] for r in ledger) == 0


def test_rows_are_shaped_like_the_books(ledger):
    assert all(tuple(r) == GL_COLUMNS for r in ledger)
    assert all(isinstance(r["amount_cents"], int) and isinstance(r["line_no"], int) for r in ledger)
    assert len({(r["entry_id"], r["line_no"]) for r in ledger}) == len(ledger)
    assert all(r["gl_period"] == r["posted_on"][:7] and r["known_on"] == r["posted_on"] for r in ledger)
    assert [r["entry_id"] for r in ledger] == sorted(r["entry_id"] for r in ledger)
    assert [r["posted_on"] for r in ledger] == sorted(r["posted_on"] for r in ledger)


def test_deferred_revenue_ties_to_the_rollforward_every_month(plan, ledger):
    rolls = tr.rollforward(INVOICES, PAYMENTS, through="2026-03")
    assert rolls.months[-1] == "2026-03"
    for month in rolls.months:
        closing = sum(r.closing for (_, m), r in rolls.defrev.items() if m == month)
        assert -balances(ledger, "2400", month) == closing, month


def test_receivables_tie_to_the_rollforward_every_month(plan, ledger):
    rolls = tr.rollforward(INVOICES, PAYMENTS, through="2026-03")
    for month in rolls.months:
        closing = sum(r.closing for (_, m), r in rolls.ar.items() if m == month)
        assert balances(ledger, "1200", month) == closing, month


def test_revenue_activity_ties_to_recognised_revenue(plan, ledger):
    rolls = tr.rollforward(INVOICES, PAYMENTS, through="2026-03")
    for month in rolls.months:
        activity = -sum(r["amount_cents"] for r in ledger
                        if r["gl_account"] in ("4000", "4100") and r["gl_period"] == month)
        assert activity == rolls.revenue_total(month), month
    assert -balances(ledger, "4100", "2026-03") == 75_000


def test_cash_ties_to_the_bank_balance_every_month(plan, costs, ledger):
    rolls = tr.cash_rollforward(bank_with(costs))
    assert len(rolls) > 12
    for month, roll in rolls.items():
        assert balances(ledger, "1000", month) == roll.closing, month


def test_invoice_lines_post_on_the_invoice_date_to_the_right_accounts(ledger):
    by_ref = defaultdict(list)
    for r in ledger:
        by_ref[r["source_ref"]].append(r)
    sub = [r for r in by_ref["inv_1"] if r["posted_on"] == "2025-01-01"]
    assert sorted((r["gl_account"], r["amount_cents"]) for r in sub) == [
        ("1200", 333_333), ("1200", 1_200_001), ("2400", -1_200_001), ("2400", -333_333)]
    assert sorted((r["gl_account"], r["amount_cents"]) for r in by_ref["inv_3"]) == [("1200", 75_000), ("4100", -75_000)]
    credit = by_ref["inv_4"]
    assert sorted((r["gl_account"], r["amount_cents"]) for r in credit) == [("1200", -250_001), ("2400", 250_001)]
    assert {r["posted_on"] for r in credit} == {"2025-03-15"}


def test_recognition_sums_to_the_line_and_posts_at_month_end(ledger):
    rec = [r for r in ledger if r["source_ref"] == "rec:inv_1:1"]
    assert {r["gl_account"] for r in rec} == {"2400", "4000"}
    assert sum(r["amount_cents"] for r in rec if r["gl_account"] == "4000") == -1_200_001
    days = {r["posted_on"] for r in rec}
    assert days == {tr.month_end(m).isoformat() for m in tr.months_between("2025-01", "2025-12")}
    jan = [r for r in rec if r["gl_period"] == "2025-01"]
    assert sorted(r["amount_cents"] for r in jan) == [-tr.ratable_cents(1_200_001, "2025-01-01", "2025-12-31", "2025-01"),
                                                     tr.ratable_cents(1_200_001, "2025-01-01", "2025-12-31", "2025-01")]


def test_recognition_is_posted_only_for_the_given_months(costs):
    rows = gl(INVOICES, PAYMENTS, bank_with(costs), costs, ["2025-01", "2025-02"])
    assert {r["gl_period"] for r in rows if r["source_ref"].startswith("rec:")} == {"2025-01", "2025-02"}


def test_payments_and_opening_balance(ledger):
    pay_1 = [r for r in ledger if r["source_ref"] == "pay_1"]
    assert sorted((r["gl_account"], r["amount_cents"]) for r in pay_1) == [("1000", 1_000_000), ("1200", -1_000_000)]
    assert {r["posted_on"] for r in pay_1} == {"2025-02-03"}
    opening = [r for r in ledger if r["source_ref"] == "bk_0001"]
    assert sorted((r["gl_account"], r["amount_cents"]) for r in opening) == [("1000", 900_000_000), ("3000", -900_000_000)]
    assert balances(ledger, "3000", "2026-03") == -900_000_000


def test_disbursements_post_to_expense_accounts(costs, ledger):
    spend = defaultdict(int)
    for r in ledger:
        if r["gl_account"].startswith("6"):
            spend[r["gl_account"]] += r["amount_cents"]
    assert set(spend) == {"6000", "6200", "6300", "6400", "6500"}
    assert sum(spend.values()) == -sum(r["amount_cents"] for r in costs.bank_rows)
    assert spend["6000"] == sum(r["cash_cents"] for r in costs.payroll_runs)
    cash_out = [r for r in ledger if r["gl_account"] == "1000" and r["source_ref"].startswith("bk_opx_")]
    assert len(cash_out) == len(costs.bank_rows)


def test_subledger_name_is_the_customers_ledger_variant(plan, costs):
    names = subledger_names(plan, rng)
    v = variants(plan, rng)
    acct = plan.customers[0].crm_account_id
    bc = v.billing_customer_ids[acct]
    assert names[bc] == v.name_for(acct, "gl")
    rows = gl([inv("inv_9", 1, bc, "2025-01-01", "2025-01-01", "2025-01-31", "subscription", 1000)],
              [pay("pay_9", bc, "inv_9", 1000, "2025-02-01", "bk_0002")], [OPENING], costs, ["2025-01"],
              subledger_names=names)
    assert {r["subledger_name"] for r in rows if r["source_ref"] in ("inv_9", "pay_9", "rec:inv_9:1")} == {names[bc]}
    unnamed = gl(INVOICES[:1], [], [OPENING], costs, ["2025-01"])
    assert {r["subledger_name"] for r in unnamed if r["source_ref"] == "inv_1"} == {"bc_a"}


def test_a_disbursement_with_no_expense_account_is_refused(costs):
    stray = dict(OPENING, bank_txn_id="bk_9999", posted_on="2025-01-05", amount_cents=-100)
    with pytest.raises(ValueError, match="bk_9999"):
        gl([], [], [OPENING, stray], Opex(), ["2025-01"])


def test_unknown_line_type_is_refused():
    with pytest.raises(ValueError, match="line_type"):
        gl([inv("inv_1", 1, "bc_a", "2025-01-01", "2025-01-01", "2025-01-31", "refund", 5)], [], [], Opex(), [])


# -- payroll ------------------------------------------------------------------------------------

def test_payroll_runs_twice_a_month_on_business_days(plan, costs):
    runs = costs.payroll_runs
    assert len(runs) == 2 * len(plan.months)
    assert all(tuple(r) == PAYROLL_COLUMNS for r in runs)
    calendar = set(plan.calendar)
    assert all(r["pay_date"] in calendar for r in runs)
    assert all(date.fromisoformat(r["pay_date"]).weekday() < 5 for r in runs)
    assert [r["pay_date"] for r in runs] == sorted(r["pay_date"] for r in runs)


def test_payroll_moves_back_to_the_previous_business_day(costs):
    dates = {(r["half"], r["pay_date"]) for r in costs.payroll_runs}
    assert ("mid", "2025-11-14") in dates        # the 15th is a Saturday
    assert ("end", "2025-05-30") in dates        # the 31st is a Saturday
    assert ("mid", "2025-01-15") in dates and ("end", "2025-03-31") in dates
    for run in costs.payroll_runs:
        month = run["pay_date"][:7]
        nominal = date(int(month[:4]), int(month[5:]), 15) if run["half"] == "mid" else tr.month_end(month)
        assert 0 <= (nominal - date.fromisoformat(run["pay_date"])).days <= 2


def test_payroll_cost_follows_headcount(plan, costs):
    per_head = plan.headcount["cost_per_head_per_run_cents"]
    by_month = {r["pay_date"][:7]: r for r in costs.payroll_runs}
    assert by_month["2024-12"]["headcount"] == 14 + 2 + 2
    assert by_month["2025-01"]["headcount"] == 14 + 2 + 2 + 3
    assert all(r["cash_cents"] == r["headcount"] * per_head for r in costs.payroll_runs)
    plan_rows = {r["month"]: r for r in costs.headcount_plan}
    assert plan_rows["2025-01"]["delta_cash_per_run_cents"] == 3 * per_head
    assert plan_rows["2025-01"]["known_on"] == "2024-12-01"
    assert plan_rows["2026-01"]["known_on"] == "2025-12-01"
    assert "2026-07" not in plan_rows                            # past the end of the truth horizon
    assert "2024-07" not in plan_rows                            # before the books


# -- commissions --------------------------------------------------------------------------------

def test_commissions_are_paid_on_the_15th_of_the_month_after(plan):
    rows = opex(plan, [dict(signed_on="2025-01-10", acv_cents=10_000_001),
                       dict(signed_on="2025-01-20", acv_cents=2_000_000)],
                [dict(applied_on="2025-01-25", applied_cents=5_000_000)], rng)
    commission = [r for r in rows.bank_rows if r["counterparty_text"] == "SALES COMMISSIONS"]
    assert len(commission) == 1
    row = commission[0]
    assert row["posted_on"] == "2025-02-14"                       # the 15th is a Saturday
    assert row["amount_cents"] == -(12_000_001 * 500 // 10_000 + 5_000_000 * 100 // 10_000)
    assert rows.expenses[row["bank_txn_id"]] == ("6100", "Commissions")
    plan_row = rows.commission_plan[0]
    assert (plan_row["rate_on_booking_bps"], plan_row["rate_on_collection_bps"]) == (500, 100)


def test_commissions_outside_the_books_are_not_paid(plan):
    rows = opex(plan, [dict(signed_on="2026-03-10", acv_cents=1_000_000), dict(signed_on="2023-01-10", acv_cents=1_000_000)],
                [], rng)
    assert not [r for r in rows.bank_rows if r["counterparty_text"] == "SALES COMMISSIONS"]


# -- vendor bills and commitments ---------------------------------------------------------------

def test_bills_are_paid_by_bank_rows_on_business_days_after_their_due_date(plan, costs):
    assert all(tuple(b) == BILL_COLUMNS for b in costs.ap_bills)
    bank = {r["bank_txn_id"]: r for r in costs.bank_rows}
    calendar, last_day = set(plan.calendar), tr.month_end(plan.last_month)
    assert len({b["bill_id"] for b in costs.ap_bills}) == len(costs.ap_bills)
    paid = [b for b in costs.ap_bills if b["paid_bank_txn_id"]]
    assert len(paid) > 40
    for b in paid:
        row = bank[b["paid_bank_txn_id"]]
        assert row["amount_cents"] == -b["amount_cents"]
        assert row["posted_on"] in calendar
        assert b["due_date"] <= row["posted_on"] <= (date.fromisoformat(b["due_date"]) + timedelta(days=3 + 4)).isoformat()
        assert b["bill_date"] <= b["due_date"]
    for b in costs.ap_bills:
        if not b["paid_bank_txn_id"]:
            assert date.fromisoformat(b["due_date"]) + timedelta(days=3) >= last_day - timedelta(days=4)


def test_bills_by_category_and_cadence(plan, costs):
    by_vendor = defaultdict(list)
    for b in costs.ap_bills:
        by_vendor[b["vendor_name"]].append(b)
    months = len(plan.months)
    assert len(by_vendor["Cirrus Compute (synthetic)"]) == months
    assert len(by_vendor["Pinewood Offices (synthetic)"]) == months
    assert [b["bill_date"] for b in by_vendor["Fieldnote Software (synthetic)"]] == ["2025-02-01", "2026-02-01"]
    assert [b["bill_date"][5:7] for b in by_vendor["Harbor Legal (synthetic)"]][:3] == ["10", "01", "04"]
    cloud = by_vendor["Cirrus Compute (synthetic)"]
    assert all(b["bill_date"] == tr.month_end(b["bill_date"][:7]).isoformat() for b in cloud)
    assert all(6_500_000 * 9000 // 10_000 <= b["amount_cents"] <= 6_500_000 * 11000 // 10_000 for b in cloud)
    assert len({b["amount_cents"] for b in cloud}) > 1
    assert {b["category"] for b in costs.ap_bills} == {"cloud", "vendor", "software", "events", "rent"}


def test_commitments_cover_rent_events_and_annual_software_ahead_of_time(plan, costs):
    assert all(tuple(c) == COMMITMENT_COLUMNS for c in costs.vendor_commitments)
    assert {c["category"] for c in costs.vendor_commitments} == {"rent", "events", "software"}
    assert all(c["known_on"] <= c["due_date"] for c in costs.vendor_commitments)
    assert all(c["known_on"] >= tr.month_start(plan.first_month).isoformat() for c in costs.vendor_commitments)
    assert max(c["due_date"] for c in costs.vendor_commitments) > tr.month_end(plan.last_month).isoformat()
    rent = [c for c in costs.vendor_commitments if c["category"] == "rent"]
    assert len(rent) == len(plan.months) + len(plan.truth_months)
    assert len({c["commitment_id"] for c in costs.vendor_commitments}) == len(costs.vendor_commitments)


def test_disbursement_rows_are_bank_rows(plan, costs):
    assert all(tuple(r) == BANK_COLUMNS for r in costs.bank_rows)
    assert all(isinstance(r["amount_cents"], int) and r["amount_cents"] < 0 for r in costs.bank_rows)
    assert set(costs.expenses) == {r["bank_txn_id"] for r in costs.bank_rows}
    assert len({r["bank_txn_id"] for r in costs.bank_rows}) == len(costs.bank_rows)
    assert [r["posted_on"] for r in costs.bank_rows] == sorted(r["posted_on"] for r in costs.bank_rows)
    last_day = tr.month_end(plan.last_month).isoformat()
    assert all(plan.first_month <= r["posted_on"][:7] and r["posted_on"] <= last_day for r in costs.bank_rows)


def test_opex_is_deterministic(plan, costs):
    assert opex(plan, [], [], rng) == costs
