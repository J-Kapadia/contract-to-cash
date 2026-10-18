"""The memo: a static page whose numbers come only from `prov.records`.

Templates never hold a figure. Every number is rendered by the `num` filter from a recorded
number id, so the page carries the id (`data-num-id`) that `c2c explain` resolves.
"""

from __future__ import annotations

from pathlib import Path

import duckdb
import jinja2
from markupsafe import Markup

from .errors import PreconditionError, UsageError

TEMPLATES = Path(__file__).parent / "templates"
BASE = "memo.html.j2"
SECTIONS = "memo"


def closed(con: duckdb.DuckDBPyConnection, month: str) -> bool:
    """Whether the month's AR roll-forward has rows, which is what closing a month builds."""
    has_table = con.execute("SELECT count(*) FROM information_schema.tables"
                            " WHERE table_schema = 'mart' AND table_name = 'fct_ar_rollforward'").fetchone()[0]
    if not has_table:
        return False
    return con.execute("SELECT count(*) FROM mart.fct_ar_rollforward WHERE month = ?", [month]).fetchone()[0] > 0


def money(cents: int) -> str:
    """Integer cents as dollars, e.g. -1234 -> "-$12.34"."""
    sign = "-" if cents < 0 else ""
    dollars, rest = divmod(abs(cents), 100)
    return f"{sign}${dollars:,}.{rest:02d}"


def _format(record: tuple) -> str:
    value_cents, value_text, unit = record
    if value_cents is None:
        return value_text or ""
    return money(value_cents) if unit == "cents" else str(value_cents)


def records(con: duckdb.DuckDBPyConnection) -> dict[str, tuple]:
    rows = con.execute("SELECT number_id, value_cents, value_text, unit FROM prov.records ORDER BY number_id").fetchall()
    return {number_id: (cents, text, unit) for number_id, cents, text, unit in rows}


def num_filter(known: dict[str, tuple]):
    def num(number_id: str) -> Markup:
        """The registered number `number_id` as a span carrying its id and raw value."""
        record = known.get(number_id)
        if record is None:
            raise UsageError(f"the memo names {number_id!r}, which has no record in the warehouse.")
        value = record[0] if record[0] is not None else record[1]
        return Markup('<span class="num" data-num-id="{}" data-value="{}">{}</span>').format(
            number_id, "" if value is None else str(value), _format(record))

    return num


def sections() -> list[str]:
    """The section templates, `templates/memo/NN_*.html.j2`, in name order."""
    return [f"{SECTIONS}/{p.name}" for p in sorted((TEMPLATES / SECTIONS).glob("[0-9][0-9]_*.html.j2"))]


def environment(con: duckdb.DuckDBPyConnection) -> jinja2.Environment:
    env = jinja2.Environment(loader=jinja2.FileSystemLoader(TEMPLATES), autoescape=True, undefined=jinja2.StrictUndefined,
                             keep_trailing_newline=True)
    env.filters["num"] = num_filter(records(con))
    return env


def customers_ar(con: duckdb.DuckDBPyConnection, month: str) -> list[dict]:
    """The AR closing number of each customer for `month`."""
    prefix = f"ar/{month}/"
    ids = [r[0] for r in con.execute("SELECT number_id FROM prov.records WHERE starts_with(number_id, ?)"
                                     " AND number_id LIKE '%/closing' ORDER BY number_id", [prefix]).fetchall()]
    return [{"customer_id": number_id[len(prefix):-len("/closing")], "number_id": number_id} for number_id in ids]


def build(con: duckdb.DuckDBPyConnection, month: str, out: str | Path) -> Path:
    """Write `<out>/<month>/index.html` and return its path."""
    if not closed(con, month):
        raise PreconditionError(f"month {month} is not closed: run `c2c close --month {month}` first.")
    env = environment(con)
    html = env.get_template(BASE).render(month=month, sections=sections(), ar_customers=customers_ar(con, month))
    target = Path(out) / month / "index.html"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(html, encoding="utf-8")
    return target
