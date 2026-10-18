"""The independent reference: exact splits, order statistics, roll-forwards, bridge and ARR."""

from __future__ import annotations

from datetime import date, timedelta

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from c2c import truth_reference as ref


def _months(start: date, end: date) -> list[str]:
    return ref.months_between(ref.month_of(start), ref.month_of(end))


# -- ratable revenue ----------------------------------------------------------------------------

def test_leap_year_period_sums_exactly_and_prices_the_leap_day():
    start, end = date(2028, 1, 1), date(2028, 12, 31)  # 366 days
    parts = {m: ref.ratable_cents(1_000_000, start, end, m) for m in _months(start, end)}
    assert sum(parts.values()) == 1_000_000
    # 29 February is a day like any other: 29/366 of the amount, floored cumulatively.
    assert parts["2028-02"] == 1_000_000 * 60 // 366 - 1_000_000 * 31 // 366


def test_period_spanning_a_leap_day_sums_exactly():
    start, end = date(2027, 7, 1), date(2028, 6, 30)
    assert sum(ref.ratable_cents(99_999, start, end, m) for m in _months(start, end)) == 99_999


def test_one_day_period_lands_whole_in_its_month():
    day = date(2026, 3, 31)
    assert ref.ratable_cents(12_345, day, day, "2026-03") == 12_345
    assert ref.ratable_cents(12_345, day, day, "2026-02") == 0
    assert ref.ratable_cents(12_345, day, day, "2026-04") == 0


def test_month_outside_the_period_gets_nothing():
    assert ref.ratable_cents(1000, "2026-05-10", "2026-06-20", "2026-04") == 0
    assert ref.ratable_cents(1000, "2026-05-10", "2026-06-20", "2026-07") == 0


def test_credit_amounts_split_exactly_too():
    start, end = date(2026, 1, 15), date(2026, 4, 14)
    assert sum(ref.ratable_cents(-1001, start, end, m) for m in _months(start, end)) == -1001


def test_backwards_period_is_rejected():
    with pytest.raises(ValueError):
        ref.ratable_cents(100, "2026-02-02", "2026-02-01", "2026-02")


@settings(max_examples=200, deadline=None)
@given(amount=st.integers(min_value=-10**12, max_value=10**12),
       start=st.dates(min_value=date(2020, 1, 1), max_value=date(2035, 12, 31)),
       length=st.integers(min_value=1, max_value=1500))
def test_months_sum_to_the_amount_for_random_periods(amount, start, length):
    end = start + timedelta(days=length - 1)
    months = _months(start, end)
    parts = [ref.ratable_cents(amount, start, end, m) for m in months]
    assert sum(parts) == amount
    # Grouping does not matter: the cumulative at a month end is the sum of the months so far.
    cut = len(months) // 2
    through = ref.month_end(months[cut])
    elapsed = (through - start).days + 1
    assert sum(parts[: cut + 1]) == amount * min(elapsed, length) // length


# -- split_exact --------------------------------------------------------------------------------

def test_split_exact_sums_to_the_total():
    parts = ref.split_exact(10_001, {"a": 3333, "b": 3333, "c": 3334})
    assert sum(parts.values()) == 10_001
    assert set(parts) == {"a", "b", "c"}


def test_split_exact_ties_go_to_the_ascending_key_whatever_the_dict_order():
    forward = ref.split_exact(100, {"a": 1, "b": 1, "c": 1})
    backward = ref.split_exact(100, {"c": 1, "b": 1, "a": 1})
    assert forward == backward == {"a": 34, "b": 33, "c": 33}
    assert ref.split_exact(101, {"c": 1, "b": 1, "a": 1}) == {"a": 34, "b": 34, "c": 33}


def test_split_exact_gives_leftover_cents_to_the_largest_remainders():
    # 10 split 5:3:2 over 10 is exact; 7 split the same leaves remainders 5, 1, 4 (of 10).
    assert ref.split_exact(10, {"x": 5000, "y": 3000, "z": 2000}) == {"x": 5, "y": 3, "z": 2}
    assert ref.split_exact(7, {"x": 5000, "y": 3000, "z": 2000}) == {"x": 4, "y": 2, "z": 1}


def test_split_exact_handles_a_negative_total_and_zero_weights():
    parts = ref.split_exact(-5, {"a": 1, "b": 1, "c": 0})
    assert sum(parts.values()) == -5 and parts["c"] == 0


def test_split_exact_rejects_empty_or_zero_weights():
    with pytest.raises(ValueError):
        ref.split_exact(10, {})
    with pytest.raises(ValueError):
        ref.split_exact(10, {"a": 0})


@settings(max_examples=100, deadline=None)
@given(total=st.integers(min_value=-10**9, max_value=10**9),
       weights=st.dictionaries(st.text(min_size=1, max_size=3), st.integers(0, 10_000),
                               min_size=1, max_size=12).filter(lambda w: sum(w.values()) > 0))
def test_split_exact_always_sums_to_the_total(total, weights):
    assert sum(ref.split_exact(total, weights).values()) == total


# -- order statistics and rates -----------------------------------------------------------------

def test_lower_median_takes_the_lower_of_the_middle_pair():
    assert ref.lower_median([4, 1, 3, 2]) == 2
    assert ref.lower_median([5, 1, 3]) == 3
    assert ref.lower_median([7]) == 7


def test_percentile_nearest_rank():
    data = [15, 20, 35, 40, 50]
    assert ref.percentile_nearest_rank(data, 30) == 20
    assert ref.percentile_nearest_rank(data, 40) == 20
    assert ref.percentile_nearest_rank(data, 50) == 35
    assert ref.percentile_nearest_rank(data, 100) == 50
    assert ref.percentile_nearest_rank(data, 0) == 15
    assert ref.percentile_nearest_rank(list(range(1, 11)), 80) == 8


@given(st.lists(st.integers(-10**6, 10**6), min_size=1, max_size=60), st.integers(0, 100))
def test_order_statistics_return_values_present_in_the_data(xs, p):
    assert ref.lower_median(xs) in xs
    assert ref.percentile_nearest_rank(xs, p) in xs


def test_order_statistics_reject_no_data():
    with pytest.raises(ValueError):
        ref.lower_median([])
    with pytest.raises(ValueError):
        ref.percentile_nearest_rank([], 50)


def test_rate_bps_rounds_down():
    assert ref.rate_bps(1, 3) == 3333
    assert ref.rate_bps(2, 3) == 6666
    assert ref.rate_bps(5, 5) == 10_000
    with pytest.raises(ValueError):
        ref.rate_bps(1, 0)


def test_weighted_cents_floors():
    assert ref.weighted_cents(999, 5000) == 499


# -- roll-forwards and the bridge on a handmade book --------------------------------------------

def _inv(line_type, amount, invoice_date, start, end, customer="bc_1", invoice_id="i"):
    return {"invoice_id": invoice_id, "billing_customer_id": customer, "invoice_date": invoice_date,
            "service_start": start, "service_end": end, "line_type": line_type, "amount_cents": amount}


INVOICES = [
    _inv("subscription", 365_000, "2026-01-01", "2026-01-01", "2026-12-31", invoice_id="i1"),  # 1000 a day
    _inv("overage", 5_000, "2026-01-31", "2026-01-01", "2026-01-31", invoice_id="i2"),
    _inv("credit", -2_800, "2026-02-01", "2026-02-01", "2026-02-28", invoice_id="i3"),
    _inv("subscription", 73_000, "2026-01-15", "2026-01-15", "2027-01-14", customer="bc_2", invoice_id="i4"),
]
PAYMENTS = [
    {"billing_customer_id": "bc_1", "applied_cents": 200_000, "applied_on": "2026-02-10"},
    {"billing_customer_id": "bc_2", "applied_cents": 73_000, "applied_on": "2026-02-20"},
]
BANK = [
    {"posted_on": "2026-01-01", "amount_cents": 1_000_000},
    {"posted_on": "2026-02-10", "amount_cents": 200_000},
    {"posted_on": "2026-02-20", "amount_cents": 73_000},
    {"posted_on": "2026-02-25", "amount_cents": -40_000},
    {"posted_on": "2026-03-05", "amount_cents": -10_000},
]


def test_rollforward_balances_per_customer_and_carries_the_closing_forward():
    rolls = ref.rollforward(INVOICES, PAYMENTS, through="2026-03")
    assert rolls.months == ["2026-01", "2026-02", "2026-03"]
    jan, feb = rolls.defrev["bc_1", "2026-01"], rolls.defrev["bc_1", "2026-02"]
    assert jan == ref.Roll(0, 365_000, 31_000, 334_000)
    # February: the credit adds -2800 and is itself recognised in full; 28 days of the subscription.
    assert feb == ref.Roll(334_000, -2_800, 28_000 - 2_800, 306_000)
    for roll in [*rolls.defrev.values(), *rolls.ar.values()]:
        assert roll.opening + roll.additions - roll.reductions == roll.closing
    assert rolls.ar["bc_1", "2026-01"] == ref.Roll(0, 370_000, 0, 370_000)
    assert rolls.ar["bc_1", "2026-02"] == ref.Roll(370_000, 0, 200_000 + 2_800, 167_200)
    assert rolls.ar["bc_2", "2026-03"].opening == rolls.ar["bc_2", "2026-02"].closing == 0
    assert rolls.revenue["bc_1", "2026-01", "overage"] == 5_000
    assert rolls.revenue["bc_1", "2026-02", "credit"] == -2_800


def test_cash_rollforward_opens_with_the_first_row_and_carries_empty_months():
    cash = ref.cash_rollforward(BANK + [{"posted_on": "2026-05-02", "amount_cents": 1}])
    assert cash["2026-01"] == ref.Roll(1_000_000, 0, 0, 1_000_000)
    assert cash["2026-02"] == ref.Roll(1_000_000, 273_000, 40_000, 1_233_000)
    assert cash["2026-04"] == ref.Roll(1_223_000, 0, 0, 1_223_000)
    assert cash["2026-05"].closing == 1_223_001


def test_bridge_conserves_on_a_handmade_month():
    rolls = ref.rollforward(INVOICES, PAYMENTS, through="2026-03")
    cash = ref.cash_rollforward(BANK)
    jan = ref.bridge("2026-01", rolls)
    assert jan["revenue"] == 31_000 + 5_000 + 17 * 200
    assert list(jan) == list(ref.BRIDGE_LINES)
    for month in rolls.months:
        lines = ref.bridge(month, rolls)
        assert lines["revenue"] + lines["defrev_movement"] == lines["net_billings"]
        assert lines["net_billings"] == ref.net_billings(INVOICES, month)
        assert lines["net_billings"] - lines["ar_movement"] == lines["receipts"]
        if month in cash:
            assert lines["receipts"] == cash[month].additions
    assert ref.bridge("2026-02", rolls)["receipts"] == 273_000


def test_rollforward_with_no_rows_is_empty():
    assert ref.rollforward([], []).months == []


def test_rollforward_rejects_an_unknown_line_type():
    with pytest.raises(ValueError):
        ref.rollforward([_inv("mystery", 1, "2026-01-01", "2026-01-01", "2026-01-31")], [])


# -- ARR ----------------------------------------------------------------------------------------

def _line(contract, version, effective, product, fee, start="2026-01-01", end="2026-12-31", acct="acct_1"):
    return {"contract_id": contract, "version": version, "crm_account_id": acct, "effective_from": effective,
            "term_start": start, "term_end": end, "product": product, "annual_fee_cents": fee}


def test_arr_uses_the_latest_version_effective_and_the_term():
    lines = [
        _line("c1", 1, "2026-01-01", "subscription", 120_000),
        _line("c1", 1, "2026-01-01", "overage", 0),
        _line("c1", 2, "2026-07-01", "subscription", 150_000),
        _line("c1", 2, "2026-07-01", "subscription", 30_000),
        _line("c2", 1, "2026-03-01", "subscription", 60_000, start="2026-03-01", end="2027-02-28", acct="acct_2"),
    ]
    assert ref.arr(lines, date(2026, 2, 28)) == {"acct_1": 120_000}
    assert ref.arr(lines, "2026-03-01") == {"acct_1": 120_000, "acct_2": 60_000}
    assert ref.arr(lines, "2026-07-01") == {"acct_1": 180_000, "acct_2": 60_000}
    assert ref.arr(lines, "2027-01-01") == {"acct_2": 60_000}
