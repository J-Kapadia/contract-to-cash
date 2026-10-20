"""The number-id grammar: every id a closed `mini` records parses, and each family accepts exactly its
own months, customers, metrics, lines and buckets."""

from __future__ import annotations

from pathlib import Path

import pytest

from c2c import db, ids

from conftest import MINI_BOOKS, c2c, shared_dir

MONTH = "2026-03"
FAMILIES = ["cust", "rev", "defrev", "ar", "cash", "tie", "aging", "dtp", "fc", "rate", "bt", "bridge", "chg",
            "rollup", "drift", "master", "ref", "cfg"]


@pytest.fixture(scope="module")
def warehouse(tmp_path_factory) -> Path:
    def build(directory: Path) -> None:
        done = c2c("close", "--month", MONTH, "--fixture", "mini", "--books", str(MINI_BOOKS),
                   "--warehouse", str(directory / "w.duckdb"), "--json", env={"C2C_BOOKS": ""})
        assert done.code == 0, done.stderr

    return shared_dir(tmp_path_factory, "id-grammar-mini", build) / "w.duckdb"


def test_every_recorded_id_of_a_closed_mini_parses(warehouse):
    with db.connect(warehouse, read_only=True) as con:
        recorded = con.execute("SELECT number_id, month FROM prov.records").fetchall()
    assert recorded
    for number_id, month in recorded:
        parsed = ids.parse(number_id)
        assert parsed.family in FAMILIES, number_id
        assert ids.is_valid(number_id), number_id
        if parsed.month_or_as_of is not None:
            assert parsed.month_or_as_of == month, number_id


VALID = [
    # cust
    "cust/2026-03/acct_0001/members", "cust/2026-03/acct_0001/arr",
    # rev, defrev, ar, cash
    "rev/2026-03/acct_0001/subscription", "rev/2026-03/acct_0001/overage", "rev/2026-03/acct_0001/credit",
    "defrev/2026-03/acct_0001/opening", "defrev/2026-03/acct_0001/billings",
    "defrev/2026-03/acct_0001/recognised", "defrev/2026-03/acct_0001/closing",
    "ar/2026-03/acct_0001/opening", "ar/2026-03/acct_0001/billed", "ar/2026-03/acct_0001/collected",
    "ar/2026-03/acct_0001/credits", "ar/2026-03/acct_0001/closing",
    "cash/2026-03/opening", "cash/2026-03/receipts", "cash/2026-03/disbursements", "cash/2026-03/closing",
    # tie, aging, dtp
    "tie/2026-03/1000", "tie/2026-03/2400", "tie/2026-03/4100",
    "aging/2026-03/current", "aging/2026-03/1-30", "aging/2026-03/91+",
    "aging/2026-03/acct_0001/61-90", "aging/2026-03/acct_0001/current",
    "dtp/2026-03/acct_0001/median", "dtp/2026-03/acct_0001/p80", "dtp/2026-03/*/median", "dtp/2026-03/*/p80",
    # fc
    "fc/2026-03-31/base/rev/2026-04/contracted", "fc/2026-03-31/downside/rev/2027-03/new",
    "fc/2026-03-31/upside/cash/W01/open_ar", "fc/2026-03-31/base/cash/W13/payroll",
    "fc/2026-03-31/base/cash/W07/opening", "fc/2026-03-31/base/cash/W07/ending",
    "fc/2026-03-31/downside/alert/first_breach_week",
    # rate
    "rate/2026-03-31/renewal/first", "rate/2026-03-31/expansion/later", "rate/2026-03-31/win/S3",
    # bt
    "bt/2026-03-31/base/cash_in_wape_bps", "bt/summary/downside/net_cash_error_cents",
    # bridge, chg, rollup, master
    "bridge/2026-03/revenue", "bridge/2026-03/defrev_movement", "bridge/2026-03/net_billings",
    "bridge/2026-03/ar_movement", "bridge/2026-03/receipts",
    "chg/2026-03/arr_new", "chg/2026-03/arr_expansion", "chg/2026-03/arr_contraction", "chg/2026-03/arr_churn",
    "rollup/2026-03/nrr_bps", "rollup/2026-03/grr_bps", "rollup/2026-03/logo_churn", "rollup/2026-03/stale_opps",
    "master/precision_auto", "master/recall_auto", "master/pairs_doubt", "master/pairs_reviewed",
    # drift
    "drift/2026-03/new_account/6100", "drift/2026-03/new_customer/crm_0007", "drift/2026-03/new_line_type/overage",
    "drift/2026-03/volume_shift/gl", "drift/2026-03/new_accounts", "drift/2026-03/new_customers",
    "drift/2026-03/new_line_types", "drift/2026-03/volume_shifts",
    # ref, cfg
    "ref/gl_accounts/6500", "ref/business_days/2023-01-01", "ref/freemail_domains/aol.com",
    "ref/customer_matches/acct_0007|bill_0003", "ref/legal_suffixes/gmbh",
    "cfg/min_cash_cents", "cfg/aging_buckets_days.0", "cfg/cases.base.cloud_up_bps", "cfg/stage_defaults_bps.S1",
]

INVALID = [
    # not an id, or a family the grammar does not list
    "", "/", "2026-03", "cust", "customer/2026-03/acct_0001/arr", "forecast/2026-03-31/base/cash/W01/payroll",
    "CUST/2026-03/acct_0001/arr", " cust/2026-03/acct_0001/arr", "cust/2026-03/acct_0001/arr ",
    "cust/2026-03/acct_0001/arr\n", "cust/2026-03/acct_0001/arr/",
    # the month: shape, range, and a date where a month belongs
    "cust/2026-13/acct_0001/arr", "cust/2026-00/acct_0001/arr", "cust/2026-3/acct_0001/arr",
    "cust/2026-03-31/acct_0001/arr", "cust/March/acct_0001/arr", "cash/2026-03-31/opening",
    # the customer
    "cust/2026-03/0001/arr", "cust/2026-03/acct_1/arr", "cust/2026-03/*/arr", "rev/2026-03/*/subscription",
    "cust/2026-03//arr", "aging/2026-03/*/current", "ar/2026-03/acct_0001",
    # metrics, lines and buckets outside the closed sets
    "cust/2026-03/acct_0001/count", "rev/2026-03/acct_0001/usage", "rev/2026-03/acct_0001/closing",
    "defrev/2026-03/acct_0001/billed", "ar/2026-03/acct_0001/billings", "ar/2026-03/acct_0001/recognised",
    "cash/2026-03/billed", "cash/2026-03/acct_0001/closing", "dtp/2026-03/acct_0001/mean",
    "dtp/2026-03/acct_0001", "tie/2026-03/9999", "tie/2026-03/cash", "tie/2026-03",
    "aging/2026-03", "aging/2026-03/soon", "aging/2026-03/1-", "aging/2026-03/acct_0001", "aging/2026-03/91plus",
    # forecast
    "fc/2026-03/base/rev/2026-04/contracted", "fc/2026-02-30/base/cash/W01/payroll",
    "fc/2026-03-31/middle/cash/W01/payroll", "fc/2026-03-31/base/rev/2026-04/actual",
    "fc/2026-03-31/base/rev/2026-04-30/contracted", "fc/2026-03-31/base/cash/W00/payroll",
    "fc/2026-03-31/base/cash/W14/payroll", "fc/2026-03-31/base/cash/W1/payroll",
    "fc/2026-03-31/base/cash/W01/salaries", "fc/2026-03-31/base/alert/first_breach_week/extra",
    "fc/2026-03-31/base/alert/shortfall", "fc/2026-03-31/base", "fc/2026-03-31/base/cash/W01",
    # rates and backtest
    "rate/2026-03-31/renewal/S1", "rate/2026-03-31/win/first", "rate/2026-03-31/win/S6", "rate/2026-03-31/churn/first",
    "rate/2026-03/renewal/first", "rate/2026-03-31/renewal",
    "bt/2026-03-31/base/accuracy", "bt/2026-03/base/cash_in_wape_bps", "bt/summary/middle/cash_in_wape_bps",
    "bt/summary/base", "bt/summary/2026-03-31/base/cash_in_wape_bps", "bt/base/cash_in_wape_bps",
    # bridge, chg, rollup, master
    "bridge/2026-03/gross_billings", "bridge/2026-03/revenue/extra", "bridge/revenue",
    "chg/2026-03/arr_total", "chg/2026-03/nrr_bps", "chg/2026-03/acct_0001/arr_new",
    "rollup/2026-03/arr_new", "rollup/2026-03/revenue", "rollup/nrr_bps",
    "master/2026-03/precision_auto", "master/precision", "master", "master/precision_auto/extra",
    # drift
    "drift/2026-03/other/x", "drift/2026-03/new_account", "drift/2026-03/new_accounts/6100",
    "drift/2026-03/new_account/a/b", "drift/new_account/6100", "drift/2026-03",
    # ref and cfg
    "ref/gl_accounts", "ref/gl_accounts/", "ref/sources/gl", "ref/gl_accounts/65 00", "ref/gl_accounts/a/b",
    "cfg", "cfg/", "cfg/min cash", "cfg/min_cash_cents/extra", "cfg/.x", "cfg/x.", "cfg/x..y", "cfg/2026-03/min_cash_cents",
]


@pytest.mark.parametrize("number_id", VALID)
def test_valid_examples_parse(number_id):
    assert ids.is_valid(number_id)
    parsed = ids.parse(number_id)
    assert parsed.family == number_id.split("/")[0]
    period = [] if parsed.month_or_as_of is None else [parsed.month_or_as_of]
    assert "/".join([parsed.family, *period, *parsed.parts]) == number_id


@pytest.mark.parametrize("number_id", INVALID)
def test_invalid_examples_are_refused(number_id):
    assert not ids.is_valid(number_id)
    with pytest.raises(ValueError, match="not a number id"):
        ids.parse(number_id)


def test_the_examples_cover_every_family():
    assert {number_id.split("/")[0] for number_id in VALID} == set(FAMILIES)
    assert set(ids.FAMILY_NAMES) == set(FAMILIES)


def test_a_parsed_id_splits_into_family_period_and_the_rest():
    assert ids.parse("ar/2026-03/acct_0004/closing") == ids.ParsedId("ar", "2026-03", ("acct_0004", "closing"))
    assert ids.parse("cash/2026-03/opening") == ids.ParsedId("cash", "2026-03", ("opening",))
    assert ids.parse("fc/2026-03-31/base/cash/W02/ending") == \
        ids.ParsedId("fc", "2026-03-31", ("base", "cash", "W02", "ending"))
    assert ids.parse("bt/summary/base/baseline_wape_bps") == ids.ParsedId("bt", None, ("summary", "base", "baseline_wape_bps"))
    assert ids.parse("master/recall_auto") == ids.ParsedId("master", None, ("recall_auto",))
    assert ids.parse("ref/gl_accounts/6500") == ids.ParsedId("ref", None, ("gl_accounts", "6500"))


@pytest.mark.parametrize("line", ids.BRIDGE_LINES)
def test_every_bridge_line_is_an_id_and_no_other_is(line):
    assert ids.is_valid(f"bridge/{MONTH}/{line}")
    assert not ids.is_valid(f"bridge/{MONTH}/{line}_total")


def test_bridge_lines_and_chg_metrics_are_the_listed_closed_sets():
    assert ids.BRIDGE_LINES == ("revenue", "defrev_movement", "net_billings", "ar_movement", "receipts")
    assert ids.CHG_METRICS == ("arr_new", "arr_expansion", "arr_contraction", "arr_churn")


def test_a_value_that_is_not_text_is_not_an_id():
    for value in (None, 7, b"cust/2026-03/acct_0001/arr", ["cust"]):
        assert not ids.is_valid(value)
