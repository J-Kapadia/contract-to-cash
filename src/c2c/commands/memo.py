"""`c2c memo`: write the month's memo page."""

from __future__ import annotations

import click

from .. import db, memo, paths
from ..envelope import Envelope
from ..errors import PreconditionError

LOCK = "shared"
DEFAULT_OUT = "site"


@click.command(name="memo")
@click.option("--month", required=True, help="Month to write the memo for, YYYY-MM.")
@click.option("--out", default=DEFAULT_OUT, show_default=True, help="Directory the page is written under.")
@paths.path_options
def command(month: str, out: str) -> None:
    """Write the memo for a closed MONTH to OUT/MONTH/index.html."""
    resolved = click.get_current_context().obj
    if not resolved.warehouse.is_file():
        raise PreconditionError(f"no warehouse at {resolved.warehouse}: close {month} first.")
    con = db.connect(resolved.warehouse, read_only=True)
    try:
        page = memo.build(con, month, out)
    finally:
        con.close()
    Envelope(command="memo", status="ok", exit=0, month=month,
             message=f"wrote {page}", extra={"page": str(page)}).render(resolved.as_json)
