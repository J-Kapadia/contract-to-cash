"""`c2c close`: load the books through a month and build the models."""

from __future__ import annotations

import click

from .. import paths
from ..close import DEFAULT_REFERENCE, close_month
from ..errors import UsageError

LOCK = "exclusive"


@click.command(name="close")
@click.option("--month", required=True, help="Month to close, YYYY-MM.")
@click.option("--check", is_flag=True, help="Compare the results with the answer key.")
@click.option("--reference", default=DEFAULT_REFERENCE, show_default=True, help="Reference files directory.")
@paths.path_options
def command(month: str, check: bool, reference: str) -> None:
    """Close MONTH: load the books through it and run the models once."""
    ctx = click.get_current_context()
    if check:
        raise UsageError("--check is not available yet.")
    close_month(ctx.obj, month, reference).render(ctx.obj.as_json)
