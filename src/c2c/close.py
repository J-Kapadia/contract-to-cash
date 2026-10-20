"""Closing a month: load the books through it, build the models once, and close each month in order."""

from __future__ import annotations

import uuid
from pathlib import Path

from . import controls, db, dbt_runner, load, paths, prov, signoff, state
from .envelope import Envelope
from .errors import ControlFailure, PreconditionError

DEFAULT_REFERENCE = "data/reference"


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


def _lock_failure(month: str) -> ControlFailure:
    return ControlFailure(f"{month} stays locked: CTL-LOCK failed.", [
        {"id": "CTL-LOCK", "violating_rows": 1, "detail": {"row_hashes": [], "number_ids": []},
         "remediation": [f"{month} is locked and its books rows changed; restore the {month} rows of the books"
                         " or use a new warehouse."]}])


def _plan(wanted: list[str], previous: dict[str, dict | None], digests: dict[str, str], month: str,
          recompute: bool) -> list[str]:
    """The months to build, in order: the first whose books rows changed (or that is not closed yet) and
    every month after it, since each builds on the ones before. A locked month that would be built fails."""
    todo: list[str] = []
    for m in wanted:
        before = previous[m]
        closed = before is not None and before["status"] != state.OPEN
        forced = recompute and m == month
        if closed and before["input_digest"] == digests[m] and not todo and not forced:
            continue
        if closed and before["status"] == state.LOCKED:
            if forced:
                raise PreconditionError(f"{m} is locked; a locked month cannot be recomputed.")
            if before["input_digest"] != digests[m]:
                raise _lock_failure(m)
            continue
        todo.append(m)
    return todo


def _recorded_controls(con, month: str) -> list[dict]:
    """The controls of the latest run that recorded results for `month`."""
    rows = con.execute(
        "SELECT control_id, status, violating_rows FROM ctl.control_results WHERE month = ? AND run_id = ("
        " SELECT run_id FROM ctl.control_results WHERE month = ? ORDER BY rowid DESC LIMIT 1)"
        " ORDER BY rowid", [month, month]).fetchall()
    return [{"id": i, "status": st, "violating_rows": n} for i, st, n in rows]


def close_month(resolved: paths.Paths, month: str, reference: str | Path = DEFAULT_REFERENCE,
                recompute: bool = False) -> Envelope:
    """Close every month from the books' first through `month`, in order, with one dbt run.

    A month that is closed and whose books rows are unchanged is skipped; one that changed is
    rebuilt, and so is every month after it. The first month to fail its controls stops the run
    (exit 3) and stays open, with every later month it was to build; earlier months stay closed.
    A locked month whose books rows changed fails with CTL-LOCK. Refuses with exit 4 unless the
    configuration in force is signed.

    The state and provenance tables exist before the controls run so a control may read what
    earlier closes recorded."""
    load.check_month(month)
    first, _ = load.check_in_window(resolved.books, month)
    signoff.check(resolved.config, resolved.signoff)  # before the warehouse is opened

    run_id = uuid.uuid4().hex
    wanted = list(state.months(first, month))
    con = db.connect(resolved.warehouse)
    try:
        db.init_schemas(con)
        prov.init(con)
        state.check_books(con, resolved.books)
        counts = load.load_months(con, resolved.books, first, month, run_id)
        counts.update(load.load_reference(con, reference))
        counts.update(load.load_config(con, resolved.config))
        digests = {m: state.input_digest(con, m) for m in wanted}
        previous = {m: state.get(con, m) for m in wanted}
        for m in wanted:
            if previous[m] is None:
                state.transition(con, m, state.OPEN)
        todo = _plan(wanted, previous, digests, month, recompute)
        if not todo:
            return Envelope(command="close", status="ok", exit=0, month=month, controls=_recorded_controls(con, month),
                            result_hash=previous[month]["result_hash"], extra={"close_status": "unchanged"},
                            message=f"{month} is closed and unchanged")
        # The books must be sound before any model is built, so a model test fails only on a code defect.
        raw_results = controls.run(con, month, "raw", run_id)
    finally:
        con.close()
    dbt_runner.build(resolved.warehouse)

    con = db.connect(resolved.warehouse)
    try:
        stale = stale_review_pairs(con)
        results: list[controls.ControlResult] = []
        old_hash = new_hash = None
        for m in todo:
            try:
                results = controls.run(con, m, "mart", run_id)
            except ControlFailure as failure:
                for later in todo[todo.index(m):]:
                    if state.status(con, later) == state.CLOSED:
                        state.transition(con, later, state.OPEN)
                raise ControlFailure(f"cannot close {month}: {failure.message}", failure.failed_controls) from None
            prov.write_month(con, m, [r.id for r in raw_results + results], run_id)
            before = previous[m]
            old_hash = before["result_hash"] if before is not None and before["status"] == state.CLOSED else None
            new_hash = prov.result_hash(con, m)
            state.transition(con, m, state.CLOSED, digests[m], new_hash,
                             restated_from=old_hash if old_hash not in (None, new_hash) else None)
    finally:
        con.close()

    rows = sum(counts.values())
    all_results = raw_results + results
    return Envelope(command="close", status="ok", exit=0, month=month, result_hash=new_hash,
                    controls=[r.summary() for r in all_results],
                    extra={"close_status": "unchanged" if old_hash is not None and old_hash == new_hash else "closed",
                           "stale_review_pairs": stale},
                    message=f"closed {month}: loaded {rows} rows from {len(counts)} files, dbt build passed, "
                            f"{len(all_results)} controls passed"
                            + (f"; {len(stale)} reviewed pair(s) match no customer names any more"
                               if stale else ""))
