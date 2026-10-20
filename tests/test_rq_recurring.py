"""Recognised revenue split into recurring and non-recurring: `fct_revenue_recurring` against
`fct_revenue` and a hand computation from the `mini` CSVs, its provenance records, and a subscription
line whose contract line is blanked in a private copy of the books."""

from __future__ import annotations

import csv
import shutil
from datetime import date
from pathlib import Path

from c2c import db, dbt_runner, load

from conftest import DEFAULT_CONFIG, MINI_BOOKS, MINI_WINDOW, ROOT, c2c
from forecast_helpers import mini_closed  # noqa: F401  (fixture)

FIRST_MONTH, LAST_MONTH, OVERAGE_MONTH = "2024-10", "2026-03", "2024-11"
CLASSIFICATIONS = ("recurring", "non_recurring")


def query(warehouse: Path, sql: str, params=()):
    with db.connect(warehouse, read_only=True) as con:
        return con.execute(sql, params).fetchall()


def split_by_month(warehouse: Path) -> dict[str, dict[str, int]]:
    rows = query(warehouse, "SELECT month, classification, recognised_cents FROM mart.fct_revenue_recurring")
    months: dict[str, dict[str, int]] = {}
    for month, classification, cents in rows:
        months.setdefault(month, {})[classification] = cents
    return months


def cumulative(amount: int, start: date, end: date, through: date) -> int:
    """The amount recognised through the end of `through`, by day over the inclusive period."""
    days = (end - start).days + 1
    elapsed = min(max((through - start).days + 1, 0), days)
    return amount * elapsed // days


def month_share(line: dict, month: str) -> int:
    year, number = map(int, month.split("-"))
    first = date(year, number, 1)
    last = date(year + number // 12, number % 12 + 1, 1).toordinal() - 1
    start, end = date.fromisoformat(line["service_start"]), date.fromisoformat(line["service_end"])
    amount = int(line["amount_cents"])
    return (cumulative(amount, start, end, date.fromordinal(last))
            - cumulative(amount, start, end, date.fromordinal(first.toordinal() - 1)))


def test_the_two_parts_and_the_total_equal_the_recognised_revenue_of_every_month(mini_closed):
    months = split_by_month(mini_closed)
    revenue = dict(query(mini_closed, "SELECT month, sum(recognised_cents)::bigint FROM mart.fct_revenue GROUP BY month"))
    assert months and set(months) <= set(revenue) | {FIRST_MONTH}
    for month, parts in months.items():
        assert set(parts) == set(CLASSIFICATIONS), month
        assert parts["recurring"] + parts["non_recurring"] == revenue.get(month, 0), month


def test_the_known_overage_months_and_the_recurring_subscription_total(mini_closed):
    months = split_by_month(mini_closed)
    assert months[LAST_MONTH]["non_recurring"] == 17_350_000
    assert months[OVERAGE_MONTH]["non_recurring"] == 10_100_000
    subscription = dict(query(mini_closed, "SELECT month, sum(recognised_cents)::bigint FROM mart.fct_revenue"
                                           " WHERE line_type = 'subscription' GROUP BY month"))
    assert {m: parts["recurring"] for m, parts in months.items()} == {m: subscription.get(m, 0) for m in months}


def test_a_month_with_no_revenue_carries_both_rows_as_zero(mini_closed):
    assert split_by_month(mini_closed)[FIRST_MONTH] == {"recurring": 0, "non_recurring": 0}


def test_every_row_has_one_record_with_its_value_and_explain_resolves_it(mini_closed, tmp_path):
    months = split_by_month(mini_closed)
    recorded = {m for (m,) in query(mini_closed, "SELECT DISTINCT month FROM prov.records WHERE number_id LIKE 'recurring/%'")}
    assert LAST_MONTH in recorded
    for month in recorded:
        records = query(mini_closed, "SELECT number_id, value_cents FROM prov.records WHERE number_id LIKE ?",
                        [f"recurring/{month}/%"])
        assert sorted(records) == sorted(
            [(f"recurring/{month}/{c}", months[month][c]) for c in CLASSIFICATIONS]
            + [(f"recurring/{month}/total", sum(months[month].values()))])
    copy = tmp_path / "w.duckdb"
    shutil.copyfile(mini_closed, copy)
    done = c2c("explain", f"recurring/{LAST_MONTH}/recurring", "--json", "--books", str(MINI_BOOKS),
               "--warehouse", str(copy))
    assert done.code == 0, done.stderr
    chain = done.json["explanation"]
    assert chain["digest_matches"] is True and chain["record"]["step"] == "fct_revenue_recurring"
    assert chain["record"]["unit"] == "cents"


def test_a_subscription_line_without_a_contract_line_moves_to_non_recurring(mini_closed, tmp_path):
    with open(MINI_BOOKS / "invoices.csv", newline="", encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    first, last = f"{LAST_MONTH}-01", f"{LAST_MONTH}-31"
    chosen = next(i for i, r in enumerate(rows) if r["line_type"] == "subscription" and r["contract_line_id"]
                  and r["service_start"] <= first and r["service_end"] >= last)
    moved = month_share(rows[chosen], LAST_MONTH)
    assert moved > 0
    rows[chosen]["contract_line_id"] = ""

    books = tmp_path / "books"
    shutil.copytree(MINI_BOOKS, books)
    with open(books / "invoices.csv", "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(rows[0]), lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)
    warehouse = tmp_path / "w.duckdb"
    con = db.connect(warehouse)
    try:
        db.init_schemas(con)
        load.load_months(con, books, *MINI_WINDOW, "test")
        load.load_reference(con, ROOT / "data" / "reference")
        load.load_config(con, DEFAULT_CONFIG)
    finally:
        con.close()
    dbt_runner.build(warehouse)

    before, after = split_by_month(mini_closed)[LAST_MONTH], split_by_month(warehouse)[LAST_MONTH]
    assert after["recurring"] == before["recurring"] - moved
    assert after["non_recurring"] == before["non_recurring"] + moved
