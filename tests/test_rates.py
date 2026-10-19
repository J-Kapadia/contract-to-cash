"""Measured renewal, expansion and stage win rates on handmade books.

The `mini` warehouse's contracts, lines and pipeline snapshots are replaced by hand-made rows so the
expected basis points are worked out here, not by the SQL under test.
"""

from __future__ import annotations

import dataclasses
from datetime import date

from c2c import db, forecast, policy, prov
from conftest import DEFAULT_CONFIG
from forecast_helpers import mini_closed, mini_closed_copy  # noqa: F401  (fixtures)

AS_OF = date(2026, 1, 31)
LATER = "2026-02-20"
MIN_OBSERVATIONS = 4
POLICY = dataclasses.replace(policy.load(DEFAULT_CONFIG), min_observations=MIN_OBSERVATIONS)
STAGES = ("S1", "S2", "S3", "S4", "S5")

# (account, renewal_number, term_end, fees, fees of the renewal contract or None)
EXPIRIES = [
    ("a1", 0, "2025-06-30", 2000, 2000),
    ("a2", 0, "2025-07-31", 1000, 1500),
    ("a3", 0, "2025-08-31", 1000, 800),
    ("a4", 0, "2025-09-30", 1000, None),
    ("a5", 0, "2025-12-31", 1000, None),
    ("b1", 1, "2025-05-31", 1000, 1000),
    ("b2", 1, "2025-10-31", 1000, 1000),
    ("b3", 1, "2025-11-30", 1000, None),
    ("old", 0, "2023-12-31", 9000, None),  # expired before the cohort window
    ("open", 0, "2026-06-30", 9000, None),  # still in term
]


def insert_contract(con, account: str, renewal_number: int, term_end: str, fees: int, known_on: str = "2024-01-01"):
    contract_id = f"c_{account}_{renewal_number}"
    con.execute("INSERT INTO raw.contracts SELECT * REPLACE (? AS contract_id, '1' AS version, ? AS crm_account_id,"
                " ? AS renewal_number, ? AS term_end, ? AS known_on, ? AS _row_hash) FROM template_contract",
                [contract_id, account, str(renewal_number), term_end, known_on, "h-" + contract_id])
    con.execute("INSERT INTO raw.contract_lines SELECT * REPLACE (? AS contract_id, '1' AS version,"
                " 'subscription' AS product, ? AS annual_fee_cents, ? AS known_on, ? AS _row_hash)"
                " FROM template_line", [contract_id, str(fees), known_on, "hl-" + contract_id])


def insert_snapshot(con, opp: str, stage: str, snapshot_date: str, opp_type: str = "new", known_on: str | None = None):
    con.execute("INSERT INTO raw.pipeline_snapshots SELECT * REPLACE (? AS opp_id, ? AS stage, ? AS snapshot_date,"
                " ? AS opp_type, ? AS known_on, ? AS _row_hash) FROM template_snapshot",
                [opp, stage, snapshot_date, opp_type, known_on or snapshot_date, f"hs-{opp}-{stage}-{snapshot_date}"])


def handmade(warehouse) -> None:
    """Expiries, renewals and pipeline snapshots worked out in the module docstring's terms."""
    con = db.connect(warehouse)
    try:
        for table, name in (("contracts", "template_contract"), ("contract_lines", "template_line"),
                            ("pipeline_snapshots", "template_snapshot")):
            con.execute(f"CREATE TEMP TABLE {name} AS SELECT * FROM raw.{table} LIMIT 1")
            con.execute(f"DELETE FROM raw.{table}")
        for account, number, term_end, fees, renewed in EXPIRIES:
            insert_contract(con, account, number, term_end, fees)
            if renewed is not None:
                insert_contract(con, account, number + 1, "2027-12-31", renewed)
        # S1: five opportunities, two of them won later; S2: three, below the minimum.
        for opp in ("o1", "o2", "o3", "o4", "o5"):
            insert_snapshot(con, opp, "S1", "2025-03-31")
        for opp in ("o1", "o2", "o6"):
            insert_snapshot(con, opp, "S2", "2025-04-30")
        insert_snapshot(con, "o1", "won", "2025-06-30")
        insert_snapshot(con, "o2", "won", "2025-07-31")
        insert_snapshot(con, "o3", "lost", "2025-06-30")
        insert_snapshot(con, "o7", "S1", "2025-03-31", opp_type="renewal")  # not pipeline
        insert_snapshot(con, "o7", "won", "2025-06-30", opp_type="renewal")
        insert_snapshot(con, "o8", "S1", "2023-11-30")  # seen before the stage window
        insert_snapshot(con, "o8", "won", "2024-01-31")
    finally:
        con.close()


def rates(warehouse) -> dict[tuple[str, str], tuple[int, int, str]]:
    con = db.connect(warehouse, read_only=True)
    try:
        rows = con.execute("SELECT as_of, kind, cohort_or_stage, rate_bps, observations, fallback "
                           "FROM fc_build.fc_rates").fetchall()
    finally:
        con.close()
    assert {r[0] for r in rows} == {AS_OF}
    assert len(rows) == 9
    return {(r[1], r[2]): (r[3], r[4], r[5]) for r in rows}


def build(warehouse) -> dict[tuple[str, str], tuple[int, int, str]]:
    forecast.build(warehouse, AS_OF, POLICY)
    return rates(warehouse)


def test_rates_are_fee_weighted_by_cohort_and_count_based_by_stage(mini_closed_copy):
    handmade(mini_closed_copy)
    got = build(mini_closed_copy)

    # first: fees 6000 expiring, 3800 kept (2000 + 1000 + 800), 500 expanded.
    assert got[("renewal", "first")] == (3800 * 10000 // 6000, 5, "none")
    assert got[("expansion", "first")] == (500 * 10000 // 6000, 5, "none")
    # win: S1 saw 5 opportunities, 2 won later; a renewal opportunity and one outside the window do not count.
    assert got[("win", "S1")] == (2 * 10000 // 5, 5, "none")


def test_a_cohort_below_the_minimum_takes_the_pooled_rate_and_a_stage_its_default(mini_closed_copy):
    handmade(mini_closed_copy)
    got = build(mini_closed_copy)

    # later: three expiries, below four; both cohorts: 9000 expiring, 5800 kept, 500 expanded, 8 expiries.
    assert got[("renewal", "later")] == (5800 * 10000 // 9000, 3, "pooled")
    assert got[("expansion", "later")] == (500 * 10000 // 9000, 3, "pooled")
    defaults = dataclasses.asdict(POLICY.stage_defaults_bps)
    assert got[("win", "S2")] == (defaults["S2"], 3, "default")
    for stage in STAGES[2:]:
        assert got[("win", stage)] == (defaults[stage], 0, "default")


def test_without_enough_expiries_even_pooled_a_renewal_or_expansion_rate_is_zero(mini_closed_copy):
    handmade(mini_closed_copy)
    forecast.build(mini_closed_copy, AS_OF, dataclasses.replace(POLICY, min_observations=9))

    got = rates(mini_closed_copy)
    assert got[("renewal", "first")] == (0, 5, "default")
    assert got[("expansion", "later")] == (0, 3, "default")


def test_the_staged_inputs_carry_each_rate_under_each_case(mini_closed_copy):
    handmade(mini_closed_copy)
    got = build(mini_closed_copy)

    con = db.connect(mini_closed_copy, read_only=True)
    try:
        inputs = {(r[0], r[1]): (r[2], r[3]) for r in con.execute(
            'SELECT "case", driver, value, fallback FROM fc_build.inputs WHERE driver LIKE \'rate\\_%\' ESCAPE \'\\\'').fetchall()}
    finally:
        con.close()
    assert len(inputs) == 3 * 9
    renewal, expansion, win = (got[("renewal", "later")][0], got[("expansion", "later")][0], got[("win", "S1")][0])
    assert inputs[("base", "rate_renewal_later")] == (renewal, True)
    assert inputs[("downside", "rate_renewal_later")] == (renewal - 1000, True)
    assert inputs[("upside", "rate_renewal_later")] == (renewal + 500, True)
    assert inputs[("downside", "rate_expansion_later")] == (0, True)
    assert inputs[("upside", "rate_expansion_later")] == (expansion * 12500 // 10000, True)
    assert inputs[("downside", "rate_win_S1")] == (win * 5000 // 10000, False)
    assert inputs[("upside", "rate_win_S1")] == (min(win * 12500 // 10000, 10000), False)
    assert inputs[("base", "rate_win_S2")] == (POLICY.stage_defaults_bps.S2, True)


def test_each_rate_is_recorded_and_the_record_names_its_fallback(mini_closed_copy):
    handmade(mini_closed_copy)
    got = build(mini_closed_copy)

    con = db.connect(mini_closed_copy, read_only=True)
    try:
        records = {r[0]: r for r in con.execute(
            "SELECT number_id, value_text, unit, input_digest, input_row_count, source_query FROM prov.records "
            "WHERE number_id LIKE 'rate/%'").fetchall()}
        assert len(records) == 9
        later = records["rate/2026-01-31/renewal/later"]
        assert later[1] == f"{got[('renewal', 'later')][0]} bps, fallback pooled"
        assert later[2] == "bps"
        assert records["rate/2026-01-31/renewal/first"][1].endswith("fallback none")
        assert records["rate/2026-01-31/win/S2"][1].endswith("fallback default")
        for record in records.values():
            assert prov.source_digest(con, record[5]) == (record[3], record[4])
        assert records["rate/2026-01-31/win/S1"][4] > 0
    finally:
        con.close()


def test_no_rate_reads_a_row_known_after_the_as_of_date(mini_closed_copy):
    handmade(mini_closed_copy)
    before = build(mini_closed_copy)

    con = db.connect(mini_closed_copy)
    try:
        for name, table in (("template_contract", "contracts"), ("template_line", "contract_lines"),
                            ("template_snapshot", "pipeline_snapshots")):
            con.execute(f"CREATE TEMP TABLE {name} AS SELECT * FROM raw.{table} LIMIT 1")
        # a4's renewal and a contract that expired in the window, both signed after the as-of date;
        # o4 and o5 won, and a new opportunity seen in S1, all after it too.
        insert_contract(con, "a4", 1, "2027-12-31", 1000, known_on=LATER)
        insert_contract(con, "late", 0, "2025-11-30", 1000, known_on=LATER)
        insert_snapshot(con, "o4", "won", LATER, known_on=LATER)
        insert_snapshot(con, "o5", "won", LATER, known_on=LATER)
        insert_snapshot(con, "o9", "S1", "2026-01-31", known_on=LATER)
    finally:
        con.close()

    assert build(mini_closed_copy) == before
