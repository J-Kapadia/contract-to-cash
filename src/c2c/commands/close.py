"""`c2c close`: close every month through a month, in order."""

from __future__ import annotations

from pathlib import Path

import click

from .. import check, db, paths
from ..close import DEFAULT_REFERENCE, close_month
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
@click.option("--check", "run_check", is_flag=True, help="Compare the results with the answer key.")
@click.option("--reference", default=DEFAULT_REFERENCE, show_default=True, help="Reference files directory.")
@paths.path_options
def command(month: str, run_check: bool, reference: str) -> None:
    """Close MONTH: load the books through it, run the models once and close each month up to it in order."""
    ctx = click.get_current_context()
    resolved = ctx.obj
    if run_check:
        check.require_truth(resolved.truth)
    ensure_fixture_books(resolved)
    envelope = close_month(resolved, month, reference, recompute=False)
    if run_check:
        con = db.connect(resolved.warehouse, read_only=True)
        try:
            envelope.controls += check.close_check(con, month, resolved.truth)
        finally:
            con.close()
        envelope.message = f"{envelope.message}; matches the answer key"
    envelope.render(resolved.as_json)
