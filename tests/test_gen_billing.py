"""Invoices, usage and overage from handmade contract rows: totals, stubs, credits, dates."""

from __future__ import annotations

import calendar
from datetime import date, timedelta

import pytest

from c2c import truth_reference as tr
from c2c.gen.billing import INVOICE_COLUMNS, billing
from c2c.gen.plan import build_plan, load_config
from c2c.gen.seeds import rng
from c2c.gen.variants import variants


@pytest.fixture(scope="module")
def plan():
    cfg = load_config()
    return build_plan("mini", cfg, cfg["seed"])


def contract(cid, version=1, acct="acct_0001", effective=None, start="2024-11-01", end="2026-10-31",
             frequency="annual", terms=30):
    return dict(contract_id=cid, version=version, crm_account_id=acct, signed_on="2024-10-15",
                effective_from=effective or start, term_start=start, term_end=end,
                billing_frequency=frequency, payment_terms_days=terms, renewal_number=0)


def line(cid, version=1, line_id="L1", product="subscription", fee=0, included=0, per_unit=0):
    return dict(contract_id=cid, version=version, line_id=line_id, product=product,
                annual_fee_cents=fee, included_units=included, overage_cents_per_unit=per_unit)


def run(plan, contracts, lines):
    return billing(plan, contracts, lines, rng)


def subscription(rows, cid=None):
    return [r for r in rows if r["line_type"] == "subscription" and (cid is None or r["contract_id"] == cid)]


def test_unamended_annual_contract_bills_its_fee_once_per_year(plan):
    invoices, usage = run(plan, [contract("c1")], [line("c1", fee=1_200_001)])
    assert usage == []
    assert [r["amount_cents"] for r in invoices] == [1_200_001, 1_200_001]
    assert sum(r["amount_cents"] for r in subscription(invoices)) == 1_200_001 * 2
    first, second = invoices
    assert (first["invoice_date"], first["service_start"], first["service_end"]) == ("2024-11-01", "2024-11-01", "2025-10-31")
    assert (second["invoice_date"], second["service_start"], second["service_end"]) == ("2025-11-01", "2025-11-01", "2026-10-31")
    assert first["due_date"] == "2024-12-01"
    assert (first["gl_period"], second["gl_period"]) == ("2024-11", "2025-11")
    assert first["known_on"] == first["invoice_date"]
    assert first["line_type"] == "subscription" and first["line_no"] == 1


def test_quarterly_contract_bills_four_quarters_summing_to_the_fee(plan):
    invoices, _ = run(plan, [contract("c1", start="2025-01-01", end="2025-12-31", frequency="quarterly", terms=45)],
                      [line("c1", fee=1_000_003)])
    assert [r["invoice_date"] for r in invoices] == ["2025-01-01", "2025-04-01", "2025-07-01", "2025-10-01"]
    assert [r["service_end"] for r in invoices] == ["2025-03-31", "2025-06-30", "2025-09-30", "2025-12-31"]
    assert sum(r["amount_cents"] for r in invoices) == 1_000_003
    assert invoices[0]["due_date"] == "2025-02-15"


def test_rows_are_shaped_like_the_books(plan):
    invoices, usage = run(plan, [contract("c1"), contract("c2", acct="acct_0002")],
                          [line("c1", fee=900_000), line("c1", line_id="U", product="overage", included=10, per_unit=500),
                           line("c2", fee=700_000)])
    assert all(tuple(r) == INVOICE_COLUMNS for r in invoices)
    assert all(isinstance(r["amount_cents"], int) and isinstance(r["line_no"], int) for r in invoices)
    ids = variants(plan, rng).billing_customer_ids
    assert {r["contract_id"]: r["billing_customer_id"] for r in invoices} == {"c1": ids["acct_0001"], "c2": ids["acct_0002"]}
    assert len({(r["invoice_id"], r["line_no"]) for r in invoices}) == len(invoices)
    assert all(set(u) == {"contract_id", "month", "units", "known_on"} for u in usage)


def test_amendment_stub_is_the_remaining_share_of_the_price_change(plan):
    # A mid-month upgrade effective 2025-03-15 inside the year starting 2025-01-01.
    contracts = [contract("c1", start="2025-01-01", end="2025-12-31"),
                 contract("c1", version=2, start="2025-01-01", end="2025-12-31", effective="2025-03-15")]
    lines = [line("c1", fee=1_200_000), line("c1", version=2, fee=1_800_001)]
    invoices, _ = run(plan, contracts, lines)
    base, stub = invoices
    assert base["amount_cents"] == 1_200_000 and base["invoice_date"] == "2025-01-01"
    assert (stub["invoice_date"], stub["service_start"], stub["service_end"]) == ("2025-03-15", "2025-03-15", "2025-12-31")
    assert stub["line_type"] == "subscription" and stub["gl_period"] == "2025-03"
    change = 600_001
    elapsed = (date(2025, 3, 15) - date(2025, 1, 1)).days
    assert stub["amount_cents"] == change - change * elapsed // 365


def test_stub_on_a_month_start_equals_the_ratable_months_it_covers(plan):
    contracts = [contract("c1", start="2025-01-01", end="2025-12-31"),
                 contract("c1", version=2, start="2025-01-01", end="2025-12-31", effective="2025-07-01")]
    invoices, _ = run(plan, contracts, [line("c1", fee=1_200_000), line("c1", version=2, fee=1_800_001)])
    stub = invoices[-1]
    months = tr.months_between("2025-07", "2025-12")
    assert stub["amount_cents"] == sum(tr.ratable_cents(600_001, "2025-01-01", "2025-12-31", m) for m in months)
    # Recognised over the stub's own period it lands in exactly those months, and all of it.
    assert sum(tr.ratable_cents(stub["amount_cents"], stub["service_start"], stub["service_end"], m)
               for m in months) == stub["amount_cents"]


def test_later_periods_bill_at_the_amended_fee(plan):
    contracts = [contract("c1", start="2025-01-01", end="2026-12-31"),
                 contract("c1", version=2, start="2025-01-01", end="2026-12-31", effective="2025-07-01")]
    invoices, _ = run(plan, contracts, [line("c1", fee=1_000_000), line("c1", version=2, fee=1_500_000)])
    assert [(r["invoice_date"], r["amount_cents"]) for r in invoices if r["invoice_date"] >= "2026-01-01"] == [("2026-01-01", 1_500_000)]


def test_downgrade_produces_a_negative_credit_line(plan):
    contracts = [contract("c1", start="2025-01-01", end="2025-12-31"),
                 contract("c1", version=2, start="2025-01-01", end="2025-12-31", effective="2025-06-10")]
    invoices, _ = run(plan, contracts, [line("c1", fee=1_200_000), line("c1", version=2, fee=600_000)])
    credits = [r for r in invoices if r["line_type"] == "credit"]
    assert len(credits) == 1
    credit = credits[0]
    assert credit["amount_cents"] < 0 and credit["invoice_date"] == "2025-06-10"
    assert credit["service_end"] == "2025-12-31"
    change = 600_000
    assert credit["amount_cents"] == -(change - change * (date(2025, 6, 10) - date(2025, 1, 1)).days // 365)
    # Net billed over the year is the fee less the credit, and each stub shares one invoice.
    assert sum(r["amount_cents"] for r in invoices) == 1_200_000 + credit["amount_cents"]


def test_an_ended_line_is_credited_and_an_added_line_is_billed(plan):
    contracts = [contract("c1", start="2025-01-01", end="2025-12-31"),
                 contract("c1", version=2, start="2025-01-01", end="2025-12-31", effective="2025-04-01")]
    lines = [line("c1", line_id="A", fee=1_000_000), line("c1", version=2, line_id="B", fee=500_000)]
    stubs = {r["contract_line_id"]: r for r in run(plan, contracts, lines)[0] if r["invoice_date"] == "2025-04-01"}
    assert stubs["A"]["line_type"] == "credit" and stubs["A"]["amount_cents"] < 0
    assert stubs["B"]["line_type"] == "subscription" and stubs["B"]["amount_cents"] > 0
    assert (stubs["A"]["invoice_id"], stubs["A"]["line_no"]) != (stubs["B"]["invoice_id"], stubs["B"]["line_no"])
    assert stubs["A"]["invoice_id"] == stubs["B"]["invoice_id"]


def test_usage_is_drawn_monthly_and_overage_is_dated_on_the_month_end(plan):
    contracts = [contract("c1", start="2025-01-01", end="2025-12-31")]
    lines = [line("c1", fee=1_200_000), line("c1", line_id="U", product="overage", included=1000, per_unit=250)]
    invoices, usage = run(plan, contracts, lines)
    assert [u["month"] for u in usage] == tr.months_between("2025-01", "2025-12")
    by_month = {u["month"]: u for u in usage}
    overage = [r for r in invoices if r["line_type"] == "overage"]
    assert overage, "some month draws more than the included units"
    for r in overage:
        month = r["invoice_date"][:7]
        year, mon = map(int, month.split("-"))
        assert r["invoice_date"] == date(year, mon, calendar.monthrange(year, mon)[1]).isoformat()
        assert r["service_start"] == f"{month}-01" and r["service_end"] == r["invoice_date"]
        assert r["gl_period"] == month and r["known_on"] == r["invoice_date"]
        assert r["amount_cents"] == (by_month[month]["units"] - 1000) * 250 > 0
        assert r["due_date"] == (date.fromisoformat(r["invoice_date"]) + timedelta(days=30)).isoformat()
    assert {r["invoice_date"][:7] for r in overage} == {m for m, u in by_month.items() if u["units"] > 1000}


def test_nothing_is_dated_after_the_last_month_of_the_books(plan):
    invoices, usage = run(plan, [contract("c1", start="2025-01-01", end="2027-12-31")],
                          [line("c1", fee=1_200_000), line("c1", line_id="U", product="overage", included=10, per_unit=5)])
    assert max(r["invoice_date"] for r in invoices) <= tr.month_end(plan.last_month).isoformat()
    assert max(u["month"] for u in usage) == plan.last_month


def test_billing_is_deterministic_and_seeded(plan):
    contracts = [contract("c1", start="2025-01-01", end="2025-12-31")]
    lines = [line("c1", fee=1_200_000), line("c1", line_id="U", product="overage", included=1000, per_unit=250)]
    assert run(plan, contracts, lines) == run(plan, contracts, lines)
    cfg = load_config()
    other = build_plan("mini", cfg, cfg["seed"] + 1)
    assert run(plan, contracts, lines)[1] != run(other, contracts, lines)[1]


def test_an_account_outside_the_plan_is_an_error(plan):
    with pytest.raises(ValueError, match="acct_9999"):
        run(plan, [contract("c1", acct="acct_9999")], [line("c1", fee=1)])
