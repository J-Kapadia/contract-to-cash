"""The forecast: staging in the scratch schema `fc_build`, and publishing to `forecast.*`.

A forecast as of A is built by dbt (models tagged `forecast`) into `fc_build`, reading only the
point-in-time views over the books (`known_on` on or before A), `fc_build.inputs` and closed marts.
`publish` is the one place that writes `forecast.*`, in one transaction, and `run` is a whole forecast.
"""

from __future__ import annotations

import calendar
import dataclasses
import hashlib
import uuid
from collections.abc import Sequence
from datetime import date
from pathlib import Path

import duckdb

from . import db, dbt_runner, prov
from .policy import BPS_MAX, CASE_NAMES, Case, Policy

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


FC_FAMILY = Path(__file__).with_name("families") / "fc.sql"
RUN_STEP = "forecast"
PUBLISHED = (  # table, columns, the fc_build relation it is copied from, whether it is keyed by case
    ("inputs", "as_of, \"case\", driver, value, fallback", f"{SCRATCH_SCHEMA}.inputs", True),
    ("rates", "as_of, kind, cohort_or_stage, rate_bps, observations, fallback", f"{SCRATCH_SCHEMA}.{RATES_MODEL}", False),
    ("alerts", 'as_of, "case", first_breach_week, shortfall_cents', f"{SCRATCH_SCHEMA}.fc_alerts", True),
    ("daily", 'as_of, "case", date, line, direction, cents', f"{SCRATCH_SCHEMA}.fct_cash_daily", True),
    ("weekly", 'as_of, "case", week, week_start, week_end, line, cents, opening_cents, ending_cents',
     f"{SCRATCH_SCHEMA}.fc_cash_13w", True),
    ("revenue", 'as_of, "case", month, source, recognised_cents', f"{SCRATCH_SCHEMA}.fc_revenue", True),
)
TABLES = {
    "inputs": 'as_of DATE, "case" VARCHAR, driver VARCHAR, value BIGINT, fallback BOOLEAN',
    "rates": "as_of DATE, kind VARCHAR, cohort_or_stage VARCHAR, rate_bps BIGINT, observations INTEGER, fallback VARCHAR",
    "alerts": 'as_of DATE, "case" VARCHAR, first_breach_week VARCHAR, shortfall_cents BIGINT',
    "daily": 'as_of DATE, "case" VARCHAR, date DATE, line VARCHAR, direction VARCHAR, cents BIGINT',
    "weekly": 'as_of DATE, "case" VARCHAR, week VARCHAR, week_start DATE, week_end DATE, line VARCHAR, '
              "cents BIGINT, opening_cents BIGINT, ending_cents BIGINT",
    "revenue": 'as_of DATE, "case" VARCHAR, month VARCHAR, source VARCHAR, recognised_cents BIGINT',
    "runs": "as_of DATE, run_id VARCHAR, forecast_hash VARCHAR, cases VARCHAR[]",
}


def month_end(month: str) -> date:
    """The last day of `month` (`YYYY-MM`): the as-of date of a forecast for that month."""
    year, number = int(month[:4]), int(month[5:7])
    return date(year, number, calendar.monthrange(year, number)[1])


def forecast_hash(con: duckdb.DuckDBPyConnection, as_of: date) -> str:
    """SHA-256 over the published rows of `as_of`, table by table in a fixed order; it moves with any
    published number and not with the run that wrote them."""
    digest = hashlib.sha256()
    for table, _, _, _ in PUBLISHED:
        digest.update(f"{table}\n".encode())
        for row in con.execute(f"SELECT * FROM forecast.{table} WHERE as_of = ? ORDER BY ALL", [as_of]).fetchall():
            digest.update(("\x1f".join(map(str, row)) + "\n").encode())
    return digest.hexdigest()


def publish(con: duckdb.DuckDBPyConnection, as_of: date, cases: Sequence[str] = CASE_NAMES,
            run_id: str | None = None) -> str:
    """Replace everything published for `as_of` with the `fc_build` rows of `cases`, write the run, and
    commit, all in one transaction; then drop `fc_build`. Returns the run's `forecast_hash`.

    The rates are not per case, so every case's published rates are the same rows."""
    run_id = run_id or uuid.uuid4().hex
    wanted = list(cases)
    con.execute("CREATE SCHEMA IF NOT EXISTS forecast")
    con.execute("BEGIN")
    try:
        for table, columns in TABLES.items():
            con.execute(f"CREATE TABLE IF NOT EXISTS forecast.{table} ({columns})")
        for table in TABLES:
            con.execute(f"DELETE FROM forecast.{table} WHERE as_of = ?", [as_of])
        for table, columns, source, by_case in PUBLISHED:
            where = ' AND "case" IN (SELECT unnest(?))' if by_case else ""
            params = [as_of, wanted] if by_case else [as_of]
            con.execute(f"INSERT INTO forecast.{table} ({columns}) SELECT {columns} FROM {source} "
                        f"WHERE as_of = ?{where}", params)
        digest = forecast_hash(con, as_of)
        con.execute("INSERT INTO forecast.runs VALUES (?, ?, ?, ?)", [as_of, run_id, digest, wanted])
        con.execute("COMMIT")
    except BaseException:
        con.execute("ROLLBACK")
        raise
    drop_scratch(con)
    return digest


def register_forecast(con: duckdb.DuckDBPyConnection, as_of: date, run_id: str) -> int:
    """Write `fc/<as_of>/...` for every number published for `as_of`, replacing the earlier ones;
    returns the count."""
    prefix = f"fc/{as_of.isoformat()}/"
    rows = con.execute(FC_FAMILY.read_text(), {"as_of": as_of}).fetchall()
    con.execute("DELETE FROM prov.records WHERE starts_with(number_id, ?)", [prefix])
    for number_id, month, value_cents, value_text, unit, source_query in rows:
        prov.write_leaf(con, number_id, month, value_text if value_cents is None else value_cents, unit,
                        RUN_STEP, source_query, run_id=run_id)
    return len(rows)


def run(warehouse: str | Path, as_of: date, cases: Sequence[str], policy: Policy) -> dict:
    """Build the forecast as of `as_of` for every case, publish `cases` and record the provenance of
    what was published.

    Stage the inputs and run dbt (`build`), reopen the warehouse, publish in one transaction, then
    write the `fc/` records from what was published. Returns the run's id, `forecast_hash` and alerts."""
    build(warehouse, as_of, policy)
    run_id = uuid.uuid4().hex
    con = db.connect(warehouse)
    try:
        digest = publish(con, as_of, cases, run_id)
        prov.init(con)
        register_forecast(con, as_of, run_id)
        columns = ("case", "first_breach_week", "shortfall_cents")
        alerts = [dict(zip(columns, row)) for row in con.execute(
            'SELECT "case", first_breach_week, shortfall_cents FROM forecast.alerts WHERE as_of = ? '
            'ORDER BY "case"', [as_of]).fetchall()]
    finally:
        con.close()
    return {"run_id": run_id, "forecast_hash": digest, "alerts": alerts}
