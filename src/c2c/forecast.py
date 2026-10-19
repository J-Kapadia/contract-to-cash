"""The forecast's staging: the scratch schema `fc_build` and its inputs table.

A forecast as of A is built by dbt (models tagged `forecast`) into `fc_build`, reading only the
point-in-time views over the books (`known_on` on or before A), `fc_build.inputs` and closed marts.
Nothing here writes `forecast.*`.
"""

from __future__ import annotations

import dataclasses
from datetime import date
from pathlib import Path

import duckdb

from . import db, dbt_runner, prov
from .policy import BPS_MAX, Case, Policy

SCRATCH_SCHEMA = "fc_build"
FORECAST_TAG = "tag:forecast"
RATES_MODEL = "fc_rates"
RATE_FAMILY = Path(__file__).with_name("families") / "rate.sql"

Row = tuple[date, str, str, int, bool]  # as_of, case, driver, value (bps or cents), fallback


def case_rate(kind: str, rate_bps: int, case: Case) -> int:
    """A measured rate under a case: renewal moves down and up by the case's basis points, expansion
    and the stage win rate scale by its multiplier; the result stays within 0..10000."""
    if kind == "renewal":
        adjusted = rate_bps - case.renewal_down_bps + case.renewal_up_bps
    else:
        adjusted = rate_bps * (case.expansion_mult_bps if kind == "expansion" else case.pipeline_weight_mult_bps) // BPS_MAX
    return max(0, min(adjusted, BPS_MAX))


def stage_inputs(con: duckdb.DuckDBPyConnection, as_of: date, policy: Policy) -> list[Row]:
    """Write `fc_build.inputs` and return its rows, one per `(as_of, case, driver)`: the policy's own
    drivers (`fallback` false) and, from `fc_build.fc_rates`, each measured rate under each case as
    `rate_<kind>_<cohort or stage>`, with `fallback` true where the rate is not the cohort's own."""
    rows: list[Row] = [
        (as_of, case.name, driver.name, getattr(getattr(policy.cases, case.name), driver.name), False)
        for case in dataclasses.fields(policy.cases)
        for driver in dataclasses.fields(getattr(policy.cases, case.name))
    ]
    rates = con.execute(f"SELECT kind, cohort_or_stage, rate_bps, fallback FROM {SCRATCH_SCHEMA}.{RATES_MODEL} "
                        "WHERE as_of = ? ORDER BY kind, cohort_or_stage", [as_of]).fetchall()
    for case in dataclasses.fields(policy.cases):
        for kind, cohort_or_stage, rate_bps, fallback in rates:
            rows.append((as_of, case.name, f"rate_{kind}_{cohort_or_stage}",
                         case_rate(kind, rate_bps, getattr(policy.cases, case.name)), fallback != "none"))
    con.execute(f"CREATE SCHEMA IF NOT EXISTS {SCRATCH_SCHEMA}")
    con.execute(f'CREATE OR REPLACE TABLE {SCRATCH_SCHEMA}.inputs '
                '(as_of DATE, "case" VARCHAR, driver VARCHAR, value BIGINT, fallback BOOLEAN)')
    con.executemany(f"INSERT INTO {SCRATCH_SCHEMA}.inputs VALUES (?, ?, ?, ?, ?)", rows)
    return rows


def register_rates(con: duckdb.DuckDBPyConnection, policy: Policy) -> int:
    """Write `rate/<as_of>/<kind>/<cohort or stage>` for every row of `fc_build.fc_rates`, its text
    naming the rate and the fallback used; returns the count."""
    rows = con.execute(RATE_FAMILY.read_text(), {
        "cohort_window_months": policy.cohort_window_months,
        "stage_win_window_months": policy.stage_win_window_months,
    }).fetchall()
    for number_id, month, value_text, source_query in rows:
        prov.write_leaf(con, number_id, month, value_text, "bps", RATES_MODEL, source_query)
    return len(rows)


def rate_vars(as_of: date, policy: Policy) -> dict[str, object]:
    """The dbt variables the measured rates are computed with."""
    defaults = dataclasses.asdict(policy.stage_defaults_bps)
    return {"as_of": as_of.isoformat(), "min_observations": policy.min_observations,
            "cohort_window_months": policy.cohort_window_months,
            "stage_win_window_months": policy.stage_win_window_months,
            **{f"stage_default_{stage.lower()}_bps": bps for stage, bps in defaults.items()}}


def drop_scratch(con: duckdb.DuckDBPyConnection) -> None:
    """Drop everything in `fc_build`: a forecast never starts from a previous run's leftovers."""
    con.execute(f"DROP SCHEMA IF EXISTS {SCRATCH_SCHEMA} CASCADE")


def build(warehouse: str | Path, as_of: date, policy: Policy) -> None:
    """Drop `fc_build`, measure the rates, stage the inputs and run the rest of the `forecast`-tagged
    models for `as_of`: dbt runs twice, because the inputs hold the case-adjusted rates.

    The results stay in `fc_build`; this connection is closed before dbt opens the warehouse."""
    con = db.connect(warehouse)
    try:
        drop_scratch(con)
    finally:
        con.close()
    variables = rate_vars(as_of, policy)
    dbt_runner.build(warehouse, select=f"+{RATES_MODEL}", exclude=None, vars=variables)
    con = db.connect(warehouse)
    try:
        stage_inputs(con, as_of, policy)
        register_rates(con, policy)
    finally:
        con.close()
    dbt_runner.build(warehouse, select=FORECAST_TAG, exclude=f"+{RATES_MODEL}", vars=variables)
