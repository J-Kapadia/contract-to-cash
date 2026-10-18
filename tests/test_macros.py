"""The warehouse macros against the independent Python reference.

`ratable_cents()` and `split_exact()` are rendered by dbt and evaluated in DuckDB; hypothesis
checks them against `c2c.truth_reference` over random amounts, service periods (including
29 February and one-day periods) and months.
"""

from __future__ import annotations

from datetime import date, timedelta

import duckdb
import pytest
from hypothesis import example, given, settings
from hypothesis import strategies as st

import macro_sql
from c2c import truth_reference as ref

# Big enough to cross 32-bit and push amount x days past 64 bits only if the macro were careless.
AMOUNTS = st.integers(min_value=-(10**13), max_value=10**13)
STARTS = st.one_of(
    st.dates(min_value=date(2019, 1, 1), max_value=date(2031, 12, 31)),
    st.sampled_from([date(2020, 2, 29), date(2024, 2, 29), date(2024, 2, 28), date(2023, 12, 31),
                     date(2024, 3, 1), date(2028, 2, 29)]),
)
LENGTHS = st.one_of(st.just(1), st.integers(min_value=1, max_value=40), st.integers(min_value=1, max_value=900))
PERIODS = st.tuples(AMOUNTS, STARTS, LENGTHS)


def months_around(start: date, end: date) -> list[str]:
    """Every month of the period plus one on each side, where the share must be zero."""
    before = ref.month_of(start.replace(day=1) - timedelta(days=1))
    return ref.months_between(before, ref.next_month(ref.month_of(end)))


def share_rows(periods) -> list[tuple]:
    return [(amount, start, start + timedelta(days=length - 1), month)
            for amount, start, length in periods
            for month in months_around(start, start + timedelta(days=length - 1))]


def period_sums(periods, rows, got) -> list[int]:
    """The sum of the returned shares of each period, in period order."""
    sums, at = [], 0
    for amount, start, length in periods:
        count = len(months_around(start, start + timedelta(days=length - 1)))
        sums.append(sum(got[at:at + count]))
        at += count
    return sums


@settings(deadline=None, max_examples=150)
@given(st.lists(PERIODS, min_size=1, max_size=12))
@example([(100, date(2024, 2, 29), 1)])
@example([(1, date(2024, 2, 29), 366)])
@example([(-1000, date(2024, 2, 29), 3)])
@example([(7, date(2023, 12, 31), 1)])
def test_ratable_cents_matches_the_reference_and_sums_exactly(periods):
    rows = share_rows(periods)
    got = macro_sql.ratable_cents(rows)
    for (amount, start, end, month), cents in zip(rows, got, strict=True):
        assert cents == ref.ratable_cents(amount, start, end, month), (amount, start, end, month)
    assert period_sums(periods, rows, got) == [amount for amount, _, _ in periods]


def test_ratable_cents_by_day_across_a_leap_february():
    rows = [(3100, date(2024, 2, 28), date(2024, 3, 3), m) for m in ("2024-01", "2024-02", "2024-03", "2024-04")]
    # 28 February to 3 March is 5 days: two in February (28th and 29th), three in March.
    assert macro_sql.ratable_cents(rows) == [0, 1240, 1860, 0]


def test_ratable_cents_rejects_a_period_that_ends_before_it_starts():
    with pytest.raises(duckdb.Error, match="ends before it starts"):
        macro_sql.ratable_cents([(100, date(2024, 3, 2), date(2024, 3, 1), "2024-03")])


# -- split_exact -------------------------------------------------------------------------------

KEYS = st.lists(st.integers(min_value=0, max_value=60), min_size=1, max_size=12, unique=True)


@st.composite
def splits(draw):
    keys = draw(KEYS)
    weights = [draw(st.sampled_from([0, 1, 1, 2, 3, 3333, 5000, 10000]) | st.integers(0, 10000)) for _ in keys]
    if sum(weights) == 0:
        weights[0] = 1
    return draw(st.integers(min_value=-(10**9), max_value=10**9)), dict(zip(keys, weights, strict=True))


@settings(deadline=None, max_examples=150)
@given(st.lists(splits(), min_size=1, max_size=6), st.randoms(use_true_random=False))
@example([(100, {1: 1, 2: 1, 3: 1})], None)
@example([(1, {5: 1, 4: 1, 3: 1, 2: 1})], None)
def test_split_exact_matches_the_reference_and_sums_to_the_total(cases, rnd):
    rows = [(total, weight, f"{key:03d}", group)
            for group, (total, weights) in enumerate(cases)
            for key, weight in weights.items()]
    if rnd is not None:
        rnd.shuffle(rows)  # the result must not depend on row order
    got = macro_sql.split_exact(rows)
    by_group: dict[int, dict[str, int]] = {}
    for (_, _, key, group), cents in zip(rows, got, strict=True):
        by_group.setdefault(group, {})[key] = cents
    for group, (total, weights) in enumerate(cases):
        expected = ref.split_exact(total, {f"{k:03d}": w for k, w in weights.items()})
        assert by_group[group] == expected
        assert sum(by_group[group].values()) == total


def test_split_exact_breaks_ties_by_ascending_key():
    rows = [(100, 1, key, 1) for key in ("c", "a", "b")]
    assert macro_sql.split_exact(rows) == [33, 34, 33]


def test_split_exact_gives_a_zero_weight_nothing_unless_it_is_owed():
    rows = [(7, 3, "x", 1), (7, 0, "y", 1), (3, 1, "p", 2), (3, 1, "q", 2)]
    assert macro_sql.split_exact(rows) == [7, 0, 2, 1]
