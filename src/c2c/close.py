"""Closing a month: load the books through it, then build the models once."""

from __future__ import annotations

import uuid
from pathlib import Path

from . import controls, db, dbt_runner, load, paths, prov, signoff
from .envelope import Envelope
from .gen import generate

DEFAULT_REFERENCE = "data/reference"
FIXTURE_SEED = 1


def ensure_fixture_books(resolved: paths.Paths) -> None:
    """Generate the `small` fixture on demand when its books are not at their default place yet."""
    if resolved.fixture != "small" or resolved.books != Path(paths.FIXTURES["small"][0]):
        return
    if not (resolved.books / "window.json").is_file():
        generate("small", FIXTURE_SEED, resolved.books.parents[1])


STALE_REVIEWS_SQL = """
    SELECT m.a, m.b, m.decision
    FROM raw.ref_customer_matches AS m
    WHERE NOT EXISTS (
        SELECT 1 FROM mart.fct_match_candidates AS c
        WHERE list_sort([c.source_a, c.source_b]) = list_sort([m.a, m.b]))
    ORDER BY m.a, m.b
"""


def stale_review_pairs(con) -> list[dict]:
    """Reviewed pairs that no candidate pair matches any more (a variant was renamed or removed).

    They do no harm, so they are reported and never fail the close."""
    return [{"a": a, "b": b, "decision": decision} for a, b, decision in con.execute(STALE_REVIEWS_SQL).fetchall()]


def close_month(resolved: paths.Paths, month: str, reference: str | Path = DEFAULT_REFERENCE) -> Envelope:
    """Load every month from the books' first through `month`, run dbt once, and report.

    Refuses with exit 4 unless the configuration in force is signed."""
    load.check_month(month)
    ensure_fixture_books(resolved)
    first, _ = load.check_in_window(resolved.books, month)
    signoff.check(resolved.config, resolved.signoff)  # before the warehouse is opened

    run_id = uuid.uuid4().hex
    con = db.connect(resolved.warehouse)
    try:
        db.init_schemas(con)
        counts = load.load_months(con, resolved.books, first, month, run_id)
        counts.update(load.load_reference(con, reference))
        counts.update(load.load_config(con, resolved.config))
    finally:
        con.close()
    dbt_runner.build(resolved.warehouse)

    con = db.connect(resolved.warehouse)
    try:
        stale = stale_review_pairs(con)
        results = controls.run(con, month, "mart", run_id)
        prov.write_ar_closing(con, month, [r.id for r in results], run_id)
    finally:
        con.close()

    rows = sum(counts.values())
    return Envelope(command="close", status="ok", exit=0, month=month, controls=[r.summary() for r in results],
                    message=f"closed {month}: loaded {rows} rows from {len(counts)} files, dbt build passed, "
                            f"{len(results)} controls passed"
                            + (f"; {len(stale)} reviewed pair(s) match no customer names any more"
                               if stale else ""),
                    extra={"stale_review_pairs": stale})
