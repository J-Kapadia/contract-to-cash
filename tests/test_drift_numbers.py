"""What changed in the books: `fct_drift` against a hand computation from the `mini` CSVs, and each
of its rows (with the four counts) as a provenance record `explain` resolves."""

from __future__ import annotations

import csv
import shutil
from collections import Counter
from pathlib import Path

import pytest

from c2c import db, policy
from c2c.load import MONTH_COLUMN

from conftest import DEFAULT_CONFIG, MINI_BOOKS, c2c
from forecast_helpers import mini_closed  # noqa: F401  (fixture)

FIRST_MONTH, MONTH, LAST_MONTH = "2024-10", "2024-11", "2026-03"
COUNTS = {"new_account": "new_accounts", "new_customer": "new_customers",
          "new_line_type": "new_line_types", "volume_shift": "volume_shifts"}


def read(stem: str) -> list[dict]:
    with open(MINI_BOOKS / f"{stem}.csv", newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def months_of(stem: str) -> list[str]:
    return [row[MONTH_COLUMN[stem]][:7] for row in read(stem)]


def first_seen(stem: str, column: str) -> dict[str, tuple[str, int]]:
    """Each value of `column` with its first month and its rows in that month."""
    months: dict[str, list[str]] = {}
    for row, month in zip(read(stem), months_of(stem)):
        months.setdefault(row[column], []).append(month)
    return {key: (min(seen), seen.count(min(seen))) for key, seen in months.items()}


def previous(month: str) -> str:
    year, number = map(int, month.split("-"))
    return f"{year - 1}-12" if number == 1 else f"{year}-{number - 1:02d}"


def truncated(numerator: int, denominator: int) -> int:
    quotient = abs(numerator) // denominator
    return -quotient if numerator < 0 else quotient


def expected_rows(month: str) -> set[tuple]:
    rows = set()
    for category, stem, column in (("new_account", "gl", "gl_account"), ("new_customer", "crm_accounts", "crm_account_id"),
                                   ("new_line_type", "contract_lines", "product")):
        for key, (first, lines) in first_seen(stem, column).items():
            if first == month:
                rows.add((category, key, 1 if category == "new_customer" else lines, None, None))
    threshold = policy.load(DEFAULT_CONFIG).drift_volume_shift_bps
    for stem in MONTH_COLUMN:
        counts = Counter(months_of(stem))
        this, before = counts[month], counts[previous(month)]
        if before > 0:
            shift = truncated((this - before) * 10000, before)
            if abs(shift) >= threshold:
                rows.add(("volume_shift", stem, this, before, shift))
    return rows


def query(warehouse: Path, sql: str, params=()):
    with db.connect(warehouse, read_only=True) as con:
        return con.execute(sql, params).fetchall()


def drift_rows(warehouse: Path, month: str) -> set[tuple]:
    return set(query(warehouse, "SELECT category, key, value_cents, prior_cents, shift_bps FROM mart.fct_drift"
                     " WHERE month = ?", [month]))


def test_the_first_sightings_of_november_are_the_ones_the_csvs_hold(mini_closed):
    rows = drift_rows(mini_closed, MONTH)
    assert rows == expected_rows(MONTH)
    accounts = {key for category, key, *_ in rows if category == "new_account"}
    assert {"6100", "6400"} <= accounts
    assert {key for category, key, *_ in rows if category == "new_customer"}
    assert not {row for row in rows if row[0] == "new_line_type"}


def test_the_first_month_has_no_volume_shift(mini_closed):
    assert not [row for row in drift_rows(mini_closed, FIRST_MONTH) if row[0] == "volume_shift"]
    assert drift_rows(mini_closed, FIRST_MONTH) == expected_rows(FIRST_MONTH)


def test_a_volume_shift_is_kept_only_at_or_past_the_policy_threshold(mini_closed):
    threshold = policy.load(DEFAULT_CONFIG).drift_volume_shift_bps
    shifts = query(mini_closed, "SELECT month, shift_bps FROM mart.fct_drift WHERE category = 'volume_shift'")
    assert shifts and all(abs(shift) >= threshold for _, shift in shifts)


def test_every_row_of_the_last_month_has_one_record_with_its_value(mini_closed):
    rows = query(mini_closed, "SELECT category, key, value_cents, shift_bps FROM mart.fct_drift WHERE month = ?",
                 [LAST_MONTH])
    records = dict(query(mini_closed, "SELECT number_id, value_cents FROM prov.records WHERE number_id LIKE ?",
                         [f"drift/{LAST_MONTH}/%"]))
    items = {f"drift/{LAST_MONTH}/{category}/{key}": shift if category == "volume_shift" else value
             for category, key, value, shift in rows}
    assert records == items | {f"drift/{LAST_MONTH}/{metric}": sum(1 for r in rows if r[0] == category)
                               for category, metric in COUNTS.items()}


def test_the_four_counts_exist_for_every_closed_month_and_equal_the_item_counts(mini_closed):
    months = [m for (m,) in query(mini_closed, "SELECT DISTINCT month FROM prov.records WHERE number_id LIKE 'drift/%'"
                                  " ORDER BY month")]
    closed = [m for (m,) in query(mini_closed, "SELECT DISTINCT month FROM prov.records WHERE number_id LIKE 'rollup/%'")]
    assert closed and set(closed) <= set(months)
    for month in months:
        stored = dict(query(mini_closed, "SELECT number_id, value_cents FROM prov.records WHERE number_id LIKE ?",
                            [f"drift/{month}/%"]))
        for category, metric in COUNTS.items():
            (items,) = query(mini_closed, "SELECT count(*) FROM mart.fct_drift WHERE month = ? AND category = ?",
                             [month, category])[0]
            assert stored[f"drift/{month}/{metric}"] == items


def test_explain_resolves_a_new_account_with_a_matching_digest(mini_closed, tmp_path):
    copy = tmp_path / "w.duckdb"
    shutil.copyfile(mini_closed, copy)
    done = c2c("explain", f"drift/{MONTH}/new_account/6100", "--json", "--books", str(MINI_BOOKS), "--warehouse", str(copy))
    assert done.code == 0, done.stderr
    chain = done.json["explanation"]
    assert chain["digest_matches"] is True and chain["record"]["unit"] == "count"
    assert chain["record"]["step"] == "fct_drift"


def test_a_count_with_no_item_stands_over_no_row(mini_closed):
    (count, source), = query(mini_closed, "SELECT value_cents, source_query FROM prov.records WHERE number_id = ?",
                             [f"drift/{MONTH}/new_line_types"])
    assert count == 0 and source == "select _row_hash from raw.gl where false"
