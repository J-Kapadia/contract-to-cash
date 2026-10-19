"""The 12-month revenue forecast on `mini` as of 2026-01-31.

Forecast contracts go through the same billing schedule and `ratable_cents()` as the close's, so
the `contracted` revenue must equal what the close recognises for the contracts in force, and the
sources of a month must add up in the order of the cases.
"""

from __future__ import annotations

import csv
from datetime import date, timedelta

import pytest

import test_single_definition
from c2c import db
from conftest import MINI_BOOKS
from forecast_helpers import forecast_built, mini_closed, mini_closed_copy  # noqa: F401  (fixtures)

AS_OF = date(2026, 1, 31)
MONTHS = ["2026-02", "2026-03", "2026-04", "2026-05", "2026-06", "2026-07",
          "2026-08", "2026-09", "2026-10", "2026-11", "2026-12", "2027-01"]


@pytest.fixture
def built(mini_closed_copy) -> object:  # noqa: F811
    return forecast_built(mini_closed_copy, AS_OF)


def query(warehouse, sql: str, params=()) -> list[tuple]:
    con = db.connect(warehouse, read_only=True)
    try:
        return con.execute(sql, params).fetchall()
    finally:
        con.close()


def contract_rows() -> list[dict]:
    with open(MINI_BOOKS / "contracts.csv", newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def in_force() -> set[str]:
    """Contracts known by AS_OF whose latest known version ends on or after it."""
    latest: dict[str, dict] = {}
    for row in contract_rows():
        if date.fromisoformat(row["known_on"]) <= AS_OF and (
                row["contract_id"] not in latest or int(row["version"]) > int(latest[row["contract_id"]]["version"])):
            latest[row["contract_id"]] = row
    return {c for c, r in latest.items() if date.fromisoformat(r["term_end"]) >= AS_OF}


def test_contracted_revenue_equals_what_the_close_recognises_for_contracts_in_force(built):
    forecast = query(built, "SELECT recognised_cents FROM fc_build.fc_revenue "
                            "WHERE source = 'contracted' AND month = '2026-02' AND \"case\" = 'base'")
    assert len(forecast) == 1

    forced = in_force()
    # the close's recognised revenue by invoice line, not by customer: contracts not yet known at the
    # as-of date (or already ended) are left out the same way
    marks = ",".join("?" for _ in forced)
    actual = query(built, f"""
        SELECT sum(recognised_cents) FROM (
            SELECT fct_revenue.recognised_cents
            FROM mart.fct_revenue
            WHERE month = '2026-02' AND line_type <> 'overage'
              AND customer_id IN (SELECT crm_account_id FROM raw.contracts WHERE contract_id IN ({marks}))
        )""", sorted(forced))
    assert forecast[0][0] == actual[0][0]


def test_contracted_revenue_is_the_same_in_every_case(built):
    rows = query(built, "SELECT month, count(DISTINCT recognised_cents) FROM fc_build.fc_revenue "
                        "WHERE source = 'contracted' GROUP BY month")
    assert rows and all(distinct == 1 for _, distinct in rows)


def test_the_forecast_covers_the_twelve_months_after_the_as_of_date(built):
    months = {m for (m,) in query(built, "SELECT DISTINCT month FROM fc_build.fc_revenue")}
    assert months <= set(MONTHS)
    assert "2026-02" in months and "2027-01" in months


def test_base_revenue_is_at_least_the_downside_in_every_month(built):
    rows = query(built, 'SELECT month, "case", sum(recognised_cents) FROM fc_build.fc_revenue GROUP BY ALL')
    total = {(month, case): cents for month, case, cents in rows}
    assert {case for _, case in total} == {"base", "downside", "upside"}
    for month in MONTHS:
        assert total[(month, "base")] >= total[(month, "downside")], month
        assert total[(month, "upside")] >= total[(month, "base")], month


def test_a_renewal_starts_the_day_after_the_contract_expires(built):
    # the upside is the case with a renewal rate above zero on `mini` this early in its history
    expiring = {r["contract_id"]: date.fromisoformat(r["term_end"]) for r in contract_rows()}
    rows = query(built, "SELECT contract_id, min(service_start), min(invoice_date) FROM fc_build.fc_billing_schedule "
                        "WHERE source = 'renewal' AND \"case\" = 'upside' GROUP BY contract_id")
    assert rows
    for contract_id, start, invoiced in rows:
        assert start == invoiced == expiring[contract_id.removesuffix(":renewal")] + timedelta(days=1)

    renewal = query(built, "SELECT month FROM fc_build.fc_revenue WHERE source = 'renewal' AND \"case\" = 'upside' "
                           "ORDER BY month LIMIT 1")
    assert renewal[0][0] == min(start for _, start, _ in rows).strftime("%Y-%m")


def test_the_close_schedule_is_unchanged_by_the_forecast_branch(built):
    """The close builds `int_billing_schedule` with no as-of date: only actual rows, in `int`."""
    schemas = {s for (s,) in query(built, "SELECT table_schema FROM information_schema.tables "
                                          "WHERE table_name = 'int_billing_schedule'")}
    assert "fc_build" in schemas
    assert not query(built, "SELECT 1 FROM int.int_billing_schedule WHERE source <> 'actual' LIMIT 1")


def test_the_new_models_have_no_second_definition_of_ratable_revenue():
    for name in ("intermediate/int_billing_schedule.sql", "forecast/fc_billing_schedule.sql", "forecast/fc_revenue.sql"):
        path = test_single_definition.MODELS / name
        assert not test_single_definition.violations(path.read_text()), name
