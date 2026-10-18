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
    finally:
        con.close()
    dbt_runner.build(resolved.warehouse)

    con = db.connect(resolved.warehouse)
    try:
        results = controls.run(con, month, "mart", run_id)
        prov.write_ar_closing(con, month, [r.id for r in results], run_id)
    finally:
        con.close()

    rows = sum(counts.values())
    return Envelope(command="close", status="ok", exit=0, month=month, controls=[r.summary() for r in results],
                    message=f"closed {month}: loaded {rows} rows from {len(counts)} files, dbt build passed, "
                            f"{len(results)} controls passed")
