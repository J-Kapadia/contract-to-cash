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

from . import db, dbt_runner
from .policy import Policy

SCRATCH_SCHEMA = "fc_build"
FORECAST_TAG = "tag:forecast"

Row = tuple[date, str, str, int, bool]  # as_of, case, driver, value (bps or cents), fallback


def stage_inputs(con: duckdb.DuckDBPyConnection, as_of: date, policy: Policy) -> list[Row]:
    """Write one `fc_build.inputs` row per `(as_of, case, driver)` from the policy's cases and
    return the rows; every value here is the policy's own, so `fallback` is false."""
    rows: list[Row] = [
        (as_of, case.name, driver.name, getattr(getattr(policy.cases, case.name), driver.name), False)
        for case in dataclasses.fields(policy.cases)
        for driver in dataclasses.fields(getattr(policy.cases, case.name))
    ]
    con.execute(f"CREATE SCHEMA IF NOT EXISTS {SCRATCH_SCHEMA}")
    con.execute(f'CREATE OR REPLACE TABLE {SCRATCH_SCHEMA}.inputs '
                '(as_of DATE, "case" VARCHAR, driver VARCHAR, value BIGINT, fallback BOOLEAN)')
    con.executemany(f"INSERT INTO {SCRATCH_SCHEMA}.inputs VALUES (?, ?, ?, ?, ?)", rows)
    return rows


def drop_scratch(con: duckdb.DuckDBPyConnection) -> None:
    """Drop everything in `fc_build`: a forecast never starts from a previous run's leftovers."""
    con.execute(f"DROP SCHEMA IF EXISTS {SCRATCH_SCHEMA} CASCADE")


def build(warehouse: str | Path, as_of: date, policy: Policy) -> None:
    """Drop `fc_build`, stage its inputs and run the `forecast`-tagged models once for `as_of`.

    The results stay in `fc_build`; this connection is closed before dbt opens the warehouse."""
    con = db.connect(warehouse)
    try:
        drop_scratch(con)
        stage_inputs(con, as_of, policy)
    finally:
        con.close()
    dbt_runner.build(warehouse, select=FORECAST_TAG, exclude=None, vars={"as_of": as_of.isoformat()})
