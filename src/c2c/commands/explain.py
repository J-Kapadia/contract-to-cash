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


def _lines(explanation: Explanation, indent: str = "") -> list[str]:
    record = explanation.record
    status = "matches" if explanation.own_match else "MISMATCH"
    unit = "child(ren)" if explanation.children or not record["source_query"] else "row(s)"
    lines = [
        f"{indent}{record['number_id']}",
        f"{indent}  value:   {_value(record)} {record['unit']}",
        f"{indent}  step:    {record['step']}",
        f"{indent}  digest:  {record['input_digest']} over {record['input_row_count']} {unit}, re-derived {status}",
        f"{indent}  checks:  {', '.join(record['checks_passed']) or 'none'}",
        f"{indent}  git sha: {record['git_sha']}",
    ]
    for source in explanation.sources:
        lines.append(f"{indent}  {source.table} ({len(source.rows)} row(s)): {', '.join(source.columns)}")
        lines.extend(f"{indent}    " + " | ".join("" if cell is None else str(cell) for cell in row)
                     for row in source.rows)
    for child in explanation.children:
        lines.extend(_lines(child, indent + "    "))
    return lines


def _text(explanation: Explanation) -> str:
    return "\n".join(_lines(explanation))


@click.command(name="explain")
@click.argument("number_id")
@click.option("--depth", type=click.IntRange(min=0), default=1, show_default=True,
              help="0 prints the record only; 1 lists a leaf's source rows or a composite's children; each further level "
                   "walks the children one level deeper.")
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
        bad = ", ".join(node.record["number_id"] for node in explanation.mismatches)
        raise ProvenanceError(
            f"{number_id}: the stored digest of {bad} does not match the digest re-derived from its inputs; "
            "close the month again.")
    if resolved.as_json:
        envelope = Envelope(command="explain", status="ok", exit=0, month=explanation.record["month"],
                            message=f"{number_id}: digest matches", extra={"explanation": explanation.to_dict()})
        envelope.render(True)
    else:
        click.echo(_text(explanation))
