"""`c2c backtest`: re-run the forecast as of earlier month-ends and score it against the books."""

from __future__ import annotations

import click

from .. import backtest, load, paths, policy, signoff
from ..envelope import Envelope
from ..errors import UsageError

LOCK = "exclusive"


@click.command(name="backtest")
@click.option("--from", "from_", help="First as-of month to consider, YYYY-MM.")
@click.option("--to", "to", help="Last as-of month to consider, YYYY-MM.")
@paths.path_options
def command(from_: str | None, to: str | None) -> None:
    """Score the forecast as of each eligible month-end against the receipts and revenue the books show."""
    resolved = click.get_current_context().obj
    books = load.window(resolved.books)
    for month in (from_, to):
        if month is not None:
            load.check_in_window(resolved.books, month)
    if from_ and to and from_ > to:
        raise UsageError(f"--from {from_} is after --to {to}.")
    signoff.check(resolved.config, resolved.signoff)  # before the warehouse is opened
    done = backtest.run(resolved.warehouse, policy.load(resolved.config), books, from_, to)
    scored = sorted({r["as_of"] for r in done["rows"]})
    text = f"backtest scored {len(scored)} as-of date(s)" + (f": {', '.join(scored)}" if scored else "")
    if done["skipped"]:
        text += "; skipped " + ", ".join(f"{s['month']} ({s['reason']})" for s in done["skipped"])
    Envelope(command="backtest", status="ok", exit=0, message=text,
             extra={"rows": done["rows"], "skipped": done["skipped"]}).render(resolved.as_json)
