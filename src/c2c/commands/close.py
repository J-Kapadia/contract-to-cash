"""`c2c close`: close every month through a month, in order."""

from __future__ import annotations

from pathlib import Path

import click

from .. import paths
from ..close import DEFAULT_REFERENCE, close_month
from ..errors import UsageError
from ..gen import generate

LOCK = "exclusive"
FIXTURE_SEED = 1


def ensure_fixture_books(resolved: paths.Paths) -> None:
    """Generate the `small` fixture on demand when its books are not at their default place yet."""
    if resolved.fixture != "small" or resolved.books != Path(paths.FIXTURES["small"][0]):
        return
    if not (resolved.books / "window.json").is_file():
        generate("small", FIXTURE_SEED, resolved.books.parents[1])


@click.command(name="close")
@click.option("--month", required=True, help="Month to close, YYYY-MM.")
@click.option("--check", is_flag=True, help="Compare the results with the answer key.")
@click.option("--reference", default=DEFAULT_REFERENCE, show_default=True, help="Reference files directory.")
@paths.path_options
def command(month: str, check: bool, reference: str) -> None:
    """Close MONTH: load the books through it, run the models once and close each month up to it in order."""
    ctx = click.get_current_context()
    if check:
        raise UsageError("--check is not available yet.")
    ensure_fixture_books(ctx.obj)
    close_month(ctx.obj, month, reference, recompute=False).render(ctx.obj.as_json)
