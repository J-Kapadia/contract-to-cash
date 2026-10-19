"""`c2c master`: build the customer master and report its matches and doubts."""

from __future__ import annotations

import shutil
import tempfile
import uuid

import click

from .. import controls, db, dbt_runner, load, master, paths, signoff
from ..close import DEFAULT_REFERENCE, ensure_fixture_books
from ..envelope import Envelope
from ..errors import CheckMismatch, PreconditionError

LOCK = "exclusive"
SELECT = "+dim_customer fct_rollup"


def _run_master_controls(con, month: str, run_id: str) -> list[controls.ControlResult]:
    """The master controls alone: the other mart controls need models this command does not build."""
    with tempfile.TemporaryDirectory(prefix="c2c-controls-") as scratch:
        for path in controls.CONTROLS_DIR.glob("CTL-MASTER-*.sql"):
            shutil.copy(path, scratch)
        return controls.run(con, month, "mart", run_id, directory=scratch)


@click.command(name="master")
@click.option("--month", required=True, help="Month to build the master for, YYYY-MM.")
@click.option("--check", is_flag=True, help="Compare the matches with the answer key.")
@click.option("--reference", default=DEFAULT_REFERENCE, show_default=True, help="Reference files directory.")
@paths.path_options
def command(month: str, check: bool, reference: str) -> None:
    """Build the customer master through MONTH and report its clusters, candidate pairs and scores."""
    resolved = click.get_current_context().obj
    if check and not (resolved.truth / master.IDENTITIES_FILE).is_file():
        raise PreconditionError(f"{resolved.truth} not found: --check needs the answer key.")
    load.check_month(month)
    ensure_fixture_books(resolved)
    first, _ = load.check_in_window(resolved.books, month)
    signoff.check(resolved.config, resolved.signoff)  # before the warehouse is opened

    run_id = uuid.uuid4().hex
    con = db.connect(resolved.warehouse)
    try:
        db.init_schemas(con)
        load.load_months(con, resolved.books, first, month, run_id)
        load.load_reference(con, reference)
        load.load_config(con, resolved.config)
        raw_results = controls.run(con, month, "raw", run_id)
    finally:
        con.close()
    dbt_runner.build(resolved.warehouse, select=SELECT)

    con = db.connect(resolved.warehouse)
    try:
        results = raw_results + _run_master_controls(con, month, run_id)
        clusters = master.clusters_deciding(con, master.MERGING)
        pairs = master.candidates(con)
        extra: dict = {"clusters": [[{"system": s, "id": i} for s, i in cluster] for cluster in clusters],
                       "candidates": pairs}
        message = f"built the customer master for {month}: {len(clusters)} clusters, {len(pairs)} candidate pairs"
        scores = None
        if check:
            scores = master.score(con, master.read_identities(resolved.truth))
            master.write_rollup(con, month, scores)
            extra["scores"] = scores.to_dict()
    finally:
        con.close()

    if scores is not None:
        if scores.shortfalls():
            raise CheckMismatch("customer master differs from the answer key: " + ", ".join(scores.shortfalls())
                                + ". Review config/customer_matches.yaml against the candidate pairs.")
        message += (f"; precision {scores.precision_auto} bps and recall {scores.recall_auto} bps automatic, "
                    f"{scores.precision_reviewed} and {scores.recall_reviewed} after review")
    Envelope(command="master", status="ok", exit=0, month=month, controls=[r.summary() for r in results],
             message=message, extra=extra).render(resolved.as_json)
