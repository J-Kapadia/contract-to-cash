"""Renewal and expansion forecast contracts on `mini`: one row per expiring subscription line, case and source.

The expected rows are worked out here from the books' CSV files and the staged rate inputs, not from the
SQL under test.
"""

from __future__ import annotations

import csv
import dataclasses
from datetime import date, timedelta

from c2c import db, forecast, policy
from conftest import DEFAULT_CONFIG, MINI_BOOKS
from forecast_helpers import forecast_built, mini_closed, mini_closed_copy  # noqa: F401  (fixtures)

AS_OF = date(2026, 1, 31)
CASES = ("base", "downside", "upside")
SOURCES = ("renewal", "expansion")


def books(name: str) -> list[dict]:
    with open(MINI_BOOKS / f"{name}.csv", newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def day(text: str) -> date:
    return date.fromisoformat(text)


def twelve_months_on(d: date) -> date:
    return d.replace(year=d.year + 1)


def expiring(as_of: date = AS_OF) -> list[dict]:
    """The latest version of each contract known by `as_of` that ends within 12 months and is not yet renewed."""
    known = [c for c in books("contracts") if day(c["known_on"]) <= as_of]
    latest = {}
    for c in known:
        if c["contract_id"] not in latest or int(c["version"]) > int(latest[c["contract_id"]]["version"]):
            latest[c["contract_id"]] = c
    renewed = {(c["crm_account_id"], int(c["renewal_number"])) for c in latest.values()}
    return [c for c in latest.values()
            if as_of <= day(c["term_end"]) <= twelve_months_on(as_of)
            and (c["crm_account_id"], int(c["renewal_number"]) + 1) not in renewed]


def fee(contract: dict) -> int:
    return sum(int(line["annual_fee_cents"]) for line in books("contract_lines")
               if line["contract_id"] == contract["contract_id"] and line["version"] == contract["version"]
               and line["product"] == "subscription")


def rows(warehouse) -> list[tuple]:
    con = db.connect(warehouse, read_only=True)
    try:
        return con.execute('SELECT as_of, "case", source, customer_id, contract_id, line_id, term_start, term_end, '
                           "billing_frequency, payment_terms_days, annual_fee_cents FROM fc_build.fc_contracts_renewal "
                           'ORDER BY "case", source, contract_id').fetchall()
    finally:
        con.close()


def rates(warehouse) -> dict[tuple[str, str, str], int]:
    """`{(case, source, cohort): bps}` as staged for the build."""
    con = db.connect(warehouse, read_only=True)
    try:
        staged = con.execute("SELECT \"case\", driver, value FROM fc_build.inputs WHERE driver LIKE 'rate\\_renewal\\_%' "
                             "ESCAPE '\\' OR driver LIKE 'rate\\_expansion\\_%' ESCAPE '\\'").fetchall()
    finally:
        con.close()
    return {(case, *driver.removeprefix("rate_").split("_")): value for case, driver, value in staged}


def test_every_expiring_contract_yields_a_renewal_and_an_expansion_row_per_case(mini_closed_copy):
    forecast_built(mini_closed_copy, AS_OF)
    got = rows(mini_closed_copy)
    staged = rates(mini_closed_copy)

    contracts = expiring()
    assert {c["contract_id"] for c in contracts} == {
        "ctr_0001_01", "ctr_0002_00", "ctr_0006_01", "ctr_0007_00", "ctr_0009_00", "ctr_0010_00"}
    assert {r[0] for r in got} == {AS_OF}
    assert len(got) == len(contracts) * len(CASES) * len(SOURCES)
    by_key = {(r[1], r[2], r[4]): r for r in got}
    assert len(by_key) == len(got)
    for case in CASES:
        for source in SOURCES:
            for c in contracts:
                cohort = "first" if c["renewal_number"] == "0" else "later"
                end = day(c["term_end"])
                row = by_key[(case, source, f"{c['contract_id']}:{source}")]
                assert row[3] == c["crm_account_id"]
                assert row[5] == f"{c['contract_id']}_L1:{source}"
                assert row[6] == end + timedelta(days=1)
                assert row[7] == twelve_months_on(end + timedelta(days=1)) - timedelta(days=1)
                assert row[8] == c["billing_frequency"]
                assert row[9] == int(c["payment_terms_days"])
                assert row[10] == fee(c) * staged[(case, source, cohort)] // 10000


def test_the_downside_renewal_fee_is_the_base_rate_less_the_case_adjustment(mini_closed_copy):
    # with the default minimum `mini` has too few expiries to measure a rate, so the minimum is lowered to one
    forecast.build(mini_closed_copy, AS_OF, dataclasses.replace(policy.load(DEFAULT_CONFIG), min_observations=1))
    got = {(r[1], r[2], r[4]): r[10] for r in rows(mini_closed_copy)}
    staged = rates(mini_closed_copy)

    base_rate = staged[("base", "renewal", "first")]
    assert base_rate > 1000, "the fixture's measured rate leaves room for the downside adjustment"
    acv = fee(next(c for c in expiring() if c["contract_id"] == "ctr_0002_00"))
    assert staged[("downside", "renewal", "first")] == base_rate - 1000
    assert got[("downside", "renewal", "ctr_0002_00:renewal")] == acv * (base_rate - 1000) // 10000
    assert got[("base", "renewal", "ctr_0002_00:renewal")] == acv * base_rate // 10000


def test_a_contract_that_expired_before_the_as_of_date_or_is_already_renewed_is_absent(mini_closed_copy):
    forecast_built(mini_closed_copy, AS_OF)
    contract_ids = {r[4].split(":")[0] for r in rows(mini_closed_copy)}

    assert "ctr_0004_00" not in contract_ids  # ended 2025-10-31
    assert "ctr_0001_00" not in contract_ids and "ctr_0006_00" not in contract_ids  # ended and renewed
    assert "ctr_0008_00" not in contract_ids  # ends 2026-02-28 but ctr_0008_01 is known
    assert "ctr_0005_00" not in contract_ids and "ctr_0008_01" not in contract_ids  # end beyond 12 months


def test_a_contract_known_after_the_as_of_date_is_absent(mini_closed_copy):
    con = db.connect(mini_closed_copy)
    try:
        for table in ("contracts", "contract_lines"):
            con.execute(f"INSERT INTO raw.{table} SELECT * REPLACE ('ctr_late' AS contract_id, '2026-02-15' AS known_on, "
                        f"'h-late-{table}' AS _row_hash) FROM raw.{table} WHERE contract_id = 'ctr_0010_00'")
        con.execute("UPDATE raw.contracts SET renewal_number = '0', crm_account_id = 'acct_late' "
                    "WHERE contract_id = 'ctr_late'")
        # a renewal of ctr_0010_00 signed after the as-of date does not hide the expiry from the forecast
        con.execute("INSERT INTO raw.contracts SELECT * REPLACE ('ctr_0010_01' AS contract_id, '1' AS renewal_number, "
                    "'2026-02-15' AS known_on, 'h-late-renewal' AS _row_hash) FROM raw.contracts "
                    "WHERE contract_id = 'ctr_0010_00'")
    finally:
        con.close()

    forecast_built(mini_closed_copy, AS_OF)
    contract_ids = {r[4].split(":")[0] for r in rows(mini_closed_copy)}

    assert "ctr_late" not in contract_ids
    assert "ctr_0010_00" in contract_ids
