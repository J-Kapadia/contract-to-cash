"""`c2c close`: close every month through a month, in order."""

from __future__ import annotations

from pathlib import Path

import click

from .. import check, db, load, paths, state
from ..close import DEFAULT_REFERENCE, close_month
from ..errors import PreconditionError, UsageError
from ..gen import generate

LOCK = "exclusive"
FIXTURE_SEED = 1


def ensure_fixture_books(resolved: paths.Paths) -> None:
    """Generate the `small` fixture on demand when its books are not at their default place yet."""
    if resolved.fixture != "small" or resolved.books != Path(paths.FIXTURES["small"][0]):
        return
    if not (resolved.books / "window.json").is_file():
        generate("small", FIXTURE_SEED, resolved.books.parents[1])


def require_closed(resolved: paths.Paths, month: str) -> None:
    """Refuse to restate a month that is not closed: a locked month is frozen, an open one has nothing to restate."""
    current = state.OPEN
    if resolved.warehouse.is_file():
        con = db.connect(resolved.warehouse, read_only=True)
        try:
            current = state.status(con, month)
        finally:
            con.close()
    if current == state.LOCKED:
        raise PreconditionError(f"month {month} is locked; a locked month cannot be recomputed.")
    if current != state.CLOSED:
        raise PreconditionError(f"month {month} not closed; close it before recomputing.")


@click.command(name="close")
@click.option("--month", required=True, help="Month to close, YYYY-MM.")
@click.option("--check", "run_check", is_flag=True, help="Compare the results with the answer key.")
@click.option("--recompute", is_flag=True,
              help="Rebuild a closed MONTH and store the new result hash, keeping the previous one as restated.")
@click.option("--reference", default=DEFAULT_REFERENCE, show_default=True, help="Reference files directory.")
@paths.path_options
def command(month: str, run_check: bool, recompute: bool, reference: str) -> None:
    """Close MONTH: load the books through it, run the models once and close each month up to it in order."""
    ctx = click.get_current_context()
    resolved = ctx.obj
    if recompute and run_check:
        raise UsageError("--recompute and --check cannot be combined; recompute first, then check.")
    if recompute:
        load.check_month(month)
        require_closed(resolved, month)
    if run_check:
        check.require_truth(resolved.truth)
    ensure_fixture_books(resolved)
    envelope = close_month(resolved, month, reference, recompute=recompute)
    if run_check:
        con = db.connect(resolved.warehouse, read_only=True)
        try:
            envelope.controls += check.close_check(con, month, resolved.truth)
        finally:
            con.close()
        envelope.message = f"{envelope.message}; matches the answer key"
    envelope.render(resolved.as_json)
