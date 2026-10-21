"""Rolling forward to a month: close it, build the forecast as of its end, then lock the closed months
up to it and publish the forecast in one transaction."""

from __future__ import annotations

import uuid
from pathlib import Path

from . import db, forecast, paths, policy, prov, state
from .close import DEFAULT_REFERENCE, close_month
from .envelope import Envelope

TRANSACTION_WORDS = ("BEGIN", "COMMIT", "ROLLBACK")


class _Enclosed:
    """A connection whose transaction statements do nothing, so `forecast.publish` runs inside the
    transaction its caller holds and the locks and the published rows commit or roll back together."""

    def __init__(self, con) -> None:
        self._con = con

    def execute(self, sql: str, *args):
        if sql in TRANSACTION_WORDS:
            return self._con
        return self._con.execute(sql, *args)

    def __getattr__(self, name: str):
        return getattr(self._con, name)


def run(resolved: paths.Paths, month: str, reference: str | Path = DEFAULT_REFERENCE) -> Envelope:
    """Close `month` (a no-op when its books rows are unchanged), build the forecast as of its end into
    `fc_build`, then in one transaction lock every closed month up to `month`, replace the published
    forecast for that as-of date and record the run; `fc_build` is gone afterwards.

    Running it again changes nothing. A killed run leaves the months as they were and at most a
    stale `fc_build`, which the next forecast build drops first."""
    closed = close_month(resolved, month, reference)
    as_of = forecast.month_end(month)
    config = policy.load(resolved.config)
    forecast.build(resolved.warehouse, as_of, config)  # opens and closes its own connections around dbt
    run_id = uuid.uuid4().hex
    con = db.connect(resolved.warehouse)
    try:
        con.execute("BEGIN")
        try:
            state.lock_through(con, month)
            digest = forecast.publish(_Enclosed(con), as_of, policy.CASE_NAMES, run_id)
            con.execute("COMMIT")
        except BaseException:
            con.execute("ROLLBACK")
            raise
        prov.init(con)
        forecast.register_forecast(con, as_of, run_id)
        alerts = [{"case": case, "first_breach_week": week, "shortfall_cents": cents} for case, week, cents in
                  con.execute('SELECT "case", first_breach_week, shortfall_cents FROM forecast.alerts '
                              'WHERE as_of = ? ORDER BY "case"', [as_of]).fetchall()]
    finally:
        con.close()
    text = f"{closed.message}; locked through {month}; forecast as of {as_of}"
    if alerts:
        text += "; " + "; ".join(f"{a['case']} falls below minimum cash in {a['first_breach_week']}" for a in alerts)
    return Envelope(command="rollforward", status="ok", exit=0, month=month, controls=closed.controls,
                    result_hash=closed.result_hash, message=text,
                    extra={**closed.extra, "as_of": as_of.isoformat(), "cases": list(policy.CASE_NAMES),
                           "alerts": alerts, "locked_through": month, "forecast_hash": digest})
