"""`c2c explain`: print a number's provenance chain down to the source rows."""

from __future__ import annotations

import click

from .. import db, paths
from ..envelope import Envelope
from ..errors import PreconditionError, ProvenanceError
from ..explain import Explanation, chain

LOCK = "shared"


def _value(record: dict) -> str:
    return record["value_text"] if record["value_cents"] is None else str(record["value_cents"])


def _text(explanation: Explanation) -> str:
    record = explanation.record
    status = "matches" if explanation.matches else "MISMATCH"
    lines = [
        f"{record['number_id']}",
        f"  value:   {_value(record)} {record['unit']}",
        f"  step:    {record['step']}",
        f"  digest:  {record['input_digest']} over {record['input_row_count']} row(s), re-derived {status}",
        f"  checks:  {', '.join(record['checks_passed']) or 'none'}",
        f"  git sha: {record['git_sha']}",
    ]
    for source in explanation.sources:
        lines.append(f"  {source.table} ({len(source.rows)} row(s)): {', '.join(source.columns)}")
        lines.extend("    " + " | ".join("" if cell is None else cell for cell in row) for row in source.rows)
    return "\n".join(lines)


@click.command(name="explain")
@click.argument("number_id")
@click.option("--depth", type=click.IntRange(min=0), default=1, show_default=True,
              help="0 prints the record only; 1 or more also lists the source rows.")
@paths.path_options
def command(number_id: str, depth: int) -> None:
    """Print the provenance of NUMBER_ID: its value, step, digest and the source rows behind it."""
    ctx = click.get_current_context()
    resolved = ctx.obj
    if not resolved.warehouse.is_file():
        raise PreconditionError(f"no warehouse at {resolved.warehouse}: close a month first.")
    con = db.connect(resolved.warehouse, read_only=True)
    try:
        explanation = chain(con, number_id, depth)
    finally:
        con.close()
    if not explanation.matches:
        raise ProvenanceError(
            f"{number_id}: the stored digest {explanation.record['input_digest']} does not match the digest "
            f"{explanation.derived_digest} re-derived from its source rows; close the month again.")
    if resolved.as_json:
        envelope = Envelope(command="explain", status="ok", exit=0, month=explanation.record["month"],
                            message=f"{number_id}: digest matches", extra={"explanation": explanation.to_dict()})
        envelope.render(True)
    else:
        click.echo(_text(explanation))
