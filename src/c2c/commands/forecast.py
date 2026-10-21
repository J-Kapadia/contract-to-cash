"""`c2c forecast`: build and publish the forecast as of a closed month's end."""

from __future__ import annotations

import click

from .. import db, forecast, load, paths, policy, signoff, state
from ..envelope import Envelope
from ..errors import PreconditionError

LOCK = "exclusive"


@click.command(name="forecast")
@click.option("--as-of", "month", required=True, help="Month whose end the forecast is as of, YYYY-MM.")
@click.option("--case", "cases", multiple=True, type=click.Choice(policy.CASE_NAMES),
              help="Case to publish; repeat for several. All three when absent.")
@paths.path_options
def command(month: str, cases: tuple[str, ...]) -> None:
    """Publish the 13-week cash and 12-month revenue forecast as of the end of a closed MONTH."""
    resolved = click.get_current_context().obj
    load.check_month(month)
    signoff.check(resolved.config, resolved.signoff)  # before the warehouse is opened
    published = [c for c in policy.CASE_NAMES if not cases or c in cases]
    if not resolved.warehouse.is_file():
        raise PreconditionError(f"month {month} not closed: no warehouse at {resolved.warehouse}; "
                                f"run `c2c close --month {month}` first.")
    con = db.connect(resolved.warehouse, read_only=True)
    try:
        current = state.status(con, month)
    finally:
        con.close()
    if current not in (state.CLOSED, state.LOCKED):
        raise PreconditionError(f"month {month} not closed: run `c2c close --month {month}` first.")
    as_of = forecast.month_end(month)
    done = forecast.run(resolved.warehouse, as_of, published, policy.load(resolved.config))
    alerts = [a for a in done["alerts"] if a["case"] in published]
    text = f"forecast as of {as_of} for {', '.join(published)}"
    if alerts:
        text += "; " + "; ".join(f"{a['case']} falls below minimum cash in {a['first_breach_week']}" for a in alerts)
    Envelope(command="forecast", status="ok", exit=0, month=month, message=text,
             extra={"as_of": as_of.isoformat(), "cases": published, "alerts": alerts,
                    "forecast_hash": done["forecast_hash"]}).render(resolved.as_json)
