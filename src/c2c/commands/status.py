"""`c2c status`: each month's state and result hash, and the forecast runs by as-of."""

from __future__ import annotations

import datetime

import click

from .. import db, paths
from ..envelope import Envelope

LOCK = "shared"


def _plain(value):
    return value.isoformat() if isinstance(value, (datetime.date, datetime.datetime)) else value


def _has(con, schema: str, table: str) -> bool:
    return con.execute("SELECT count(*) FROM information_schema.tables WHERE table_schema = ? AND table_name = ?",
                       [schema, table]).fetchone()[0] > 0


def _months(con) -> list[dict]:
    if not _has(con, "state", "close_state"):
        return []
    rows = con.execute("SELECT month, status, result_hash, input_digest, restated_from"
                       " FROM state.close_state ORDER BY month").fetchall()
    return [dict(zip(("month", "status", "result_hash", "input_digest", "restated_from"), r)) for r in rows]


def _runs(con) -> list[dict]:
    """The rows of `forecast.runs` by as-of; empty until that table exists."""
    if not _has(con, "forecast", "runs"):
        return []
    cursor = con.execute("SELECT * FROM forecast.runs")
    columns = [d[0] for d in cursor.description]
    runs = [{c: _plain(v) for c, v in zip(columns, r)} for r in cursor.fetchall()]
    key = "as_of" if "as_of" in columns else columns[0]
    return sorted(runs, key=lambda r: tuple(str(r[c]) for c in (key, *columns)))


def _text(months: list[dict], runs: list[dict]) -> str:
    lines = ["months:"] if months else ["months: none closed yet"]
    lines += [f"  {m['month']}  {m['status']:<6}  {m['result_hash'] or '-'}" for m in months]
    lines.append("forecast runs:" if runs else "forecast runs: none")
    lines += ["  " + "  ".join(f"{k}={'-' if v is None else v}" for k, v in r.items()) for r in runs]
    return "\n".join(lines)


@click.command(name="status")
@paths.path_options
def command() -> None:
    """List the months with their state and result hash, and the forecast runs by as-of."""
    resolved = click.get_current_context().obj
    months, runs = [], []
    if resolved.warehouse.is_file():
        con = db.connect(resolved.warehouse, read_only=True)
        try:
            months, runs = _months(con), _runs(con)
        finally:
            con.close()
    text = _text(months, runs)
    if resolved.as_json:
        Envelope(command="status", status="ok", exit=0, message=text,
                 extra={"months": months, "forecast_runs": runs}).render(True)
    else:
        click.echo(text)
