"""`c2c rollforward`: close a month, lock it and the months before it, and re-forecast as of its end."""

from __future__ import annotations

import click

from .. import paths, rollforward
from ..close import DEFAULT_REFERENCE
from .close import ensure_fixture_books

LOCK = "exclusive"


@click.command(name="rollforward")
@click.option("--month", required=True, help="Month to close, lock and forecast from, YYYY-MM.")
@click.option("--reference", default=DEFAULT_REFERENCE, show_default=True, help="Reference files directory.")
@paths.path_options
def command(month: str, reference: str) -> None:
    """Close MONTH, lock it and every closed month before it, and publish the forecast as of its end."""
    resolved = click.get_current_context().obj
    ensure_fixture_books(resolved)
    rollforward.run(resolved, month, reference).render(resolved.as_json)
