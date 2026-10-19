"""`--check`: compare a closed month's results with the answer key, read-only.

The answer key is read here and nowhere else in a close. Three comparisons are made, each reported as
a named check in the same shape as a control:

- `CHK-TRUTH-ROLLFORWARD`: per customer and month, recognised revenue and the closing deferred
  revenue and receivables equal the key to the cent;
- `CHK-TRUTH-IDENTITIES`: the customer master's clusters are the key's customers;
- `CHK-PROV-COVERAGE`: every number the month's family files publish has a provenance record with
  the same value;

and, once those pass, the control `CTL-PROV` re-derives the digest of every recorded number.

A difference is `{number_id, expected, actual}` in the failed check's `detail`. The connection is
only read from, so a check changes no state and no mart.
"""

from __future__ import annotations

import csv
from collections import defaultdict
from pathlib import Path

import duckdb

from . import controls, master, prov
from .errors import C2CError, CheckMismatch, ControlFailure, PreconditionError, ProvenanceError

ROLLFORWARD_FILE = "expected_rollforward.csv"
ROLLFORWARD_ID, IDENTITIES_ID, PROV_ID = "CHK-TRUTH-ROLLFORWARD", "CHK-TRUTH-IDENTITIES", "CHK-PROV-COVERAGE"

# truth column -> (mart table, mart column, number id suffix) of the figures compared
FIGURES = {
    "revenue_cents": ("fct_revenue", "recognised_cents", ""),
    "defrev_closing_cents": ("fct_defrev_rollforward", "closing_cents", "/closing"),
    "ar_closing_cents": ("fct_ar_rollforward", "closing_cents", "/closing"),
}
NUMBER_PREFIX = {"revenue_cents": "rev", "defrev_closing_cents": "defrev", "ar_closing_cents": "ar"}
REMEDIATION = {
    ROLLFORWARD_ID: "A figure differs from the answer key: recompute the month and compare the listed numbers "
                    "with the books rows they are built from (`c2c explain <number_id>`).",
    IDENTITIES_ID: "A customer cluster differs from the answer key: review config/customer_matches.yaml "
                   "against the candidate pairs (`c2c master --check`).",
    PROV_ID: "A published number has no provenance record, or one with another value: close the month "
             "again with `c2c close --month <month> --recompute`.",
}


class CheckFailed(CheckMismatch, ControlFailure):
    """A `--check` comparison found differences: exit 5, with the failed checks in the envelope."""

    def __init__(self, message: str, failed_controls: list[dict]) -> None:
        C2CError.__init__(self, 5, message)
        self.failed_controls = failed_controls


class ProvenanceMissing(ProvenanceError, ControlFailure):
    """Numbers without a provenance record: exit 6, with the failed check in the envelope."""

    def __init__(self, message: str, failed_controls: list[dict]) -> None:
        C2CError.__init__(self, 6, message)
        self.failed_controls = failed_controls


def _failed(check_id: str, differences: list[dict]) -> dict:
    return {"id": check_id, "violating_rows": len(differences),
            "detail": {"row_hashes": [], "number_ids": [d["number_id"] for d in differences],
                       "differences": differences},
            "remediation": [REMEDIATION[check_id]]}


def _passed(check_id: str) -> dict:
    return {"id": check_id, "status": "pass", "violating_rows": 0}


def read_rollforward(truth: str | Path, month: str) -> dict[tuple[str, str], dict[str, int]]:
    """The key's figures per `(customer_id, month)` for every month up to `month`."""
    out: dict[tuple[str, str], dict[str, int]] = {}
    with (Path(truth) / ROLLFORWARD_FILE).open(encoding="utf-8", newline="") as fh:
        for row in csv.DictReader(fh):
            if row["month"] <= month:
                out[(row["customer_id"], row["month"])] = {c: int(row[c]) for c in FIGURES}
    return out


def _mart_figures(con: duckdb.DuckDBPyConnection, month: str) -> dict[tuple[str, str], dict[str, int]]:
    out: dict[tuple[str, str], dict[str, int]] = defaultdict(dict)
    for column, (table, value, _) in FIGURES.items():
        rows = con.execute(f"SELECT customer_id, month, sum({value})::bigint FROM mart.{table}"
                           " WHERE month <= ? GROUP BY ALL", [month]).fetchall()
        for customer, m, cents in rows:
            out[(str(customer), m)][column] = cents
    return out


def rollforward_differences(con: duckdb.DuckDBPyConnection, truth: str | Path, month: str) -> list[dict]:
    """Every figure that differs from the key; a customer-month on one side only counts as zero there."""
    expected = read_rollforward(truth, month)
    actual = _mart_figures(con, month)
    differences = []
    for customer, m in sorted(set(expected) | set(actual)):
        for column, (_, _, suffix) in FIGURES.items():
            want = expected.get((customer, m), {}).get(column, 0)
            got = actual.get((customer, m), {}).get(column, 0)
            if want != got:
                differences.append({"number_id": f"{NUMBER_PREFIX[column]}/{m}/{customer}{suffix}",
                                    "expected": want, "actual": got})
    return differences


def identity_differences(con: duckdb.DuckDBPyConnection, truth: str | Path) -> list[dict]:
    """Every variant whose cluster is not its true customer's variants. Variants the key does not know
    (a vendor's spellings) are left out of both sides."""
    identities = master.read_identities(truth)
    owned: dict[str, set[tuple[str, str]]] = defaultdict(set)
    for node, owner in identities.items():
        owned[owner].add(node)
    differences = []
    for cluster in master.clusters_deciding(con, master.MERGING):
        known = {node for node in cluster if node in identities}
        for node in sorted(known):
            wanted = owned[identities[node]]
            if known != wanted:
                differences.append({"number_id": f"identity/{node[0]}/{node[1]}",
                                    "expected": sorted(f"{s}/{i}" for s, i in wanted),
                                    "actual": sorted(f"{s}/{i}" for s, i in known)})
    return differences


def provenance_differences(con: duckdb.DuckDBPyConnection, month: str) -> list[dict]:
    """Every number the family files publish for each closed month up to `month` that has no record,
    or a record of another value."""
    closed = [m for (m,) in con.execute("SELECT month FROM state.close_state WHERE status <> 'open' AND month <= ?"
                                        " ORDER BY month", [month]).fetchall()]
    recorded = {n: (c, t) for n, c, t in con.execute(
        "SELECT number_id, value_cents, value_text FROM prov.records").fetchall()}
    differences = []
    for m in closed:
        for path in prov.month_families():
            for row in prov.family_rows(con, path, m):
                number_id, value = row["number_id"], (row["value_cents"], row["value_text"])
                if recorded.get(number_id) != value:
                    actual = recorded.get(number_id, (None, None))
                    differences.append({"number_id": number_id,
                                        "expected": value[0] if value[1] is None else value[1],
                                        "actual": actual[0] if actual[1] is None else actual[1]})
    return differences


def require_truth(truth: str | Path) -> None:
    """Exit 4 unless the answer key is there."""
    truth = Path(truth)
    if not (truth / ROLLFORWARD_FILE).is_file() or not (truth / master.IDENTITIES_FILE).is_file():
        raise PreconditionError(f"{truth} not found: --check needs the answer key.")


def close_check(con: duckdb.DuckDBPyConnection, month: str, truth_dir: str | Path) -> list[dict]:
    """Compare the closed `month` and the months before it with the answer key.

    Returns the checks that passed, as control summaries. Raises `CheckFailed` (exit 5) naming every
    difference, or, when only provenance is incomplete, `ProvenanceMissing` (exit 6); a digest that
    does not re-derive is `controls.ProvenanceMismatch` (exit 6)."""
    require_truth(truth_dir)
    failed = []
    for check_id, differences in ((ROLLFORWARD_ID, rollforward_differences(con, truth_dir, month)),
                                  (IDENTITIES_ID, identity_differences(con, truth_dir))):
        if differences:
            failed.append(_failed(check_id, differences))
    if failed:
        numbers = [n for f in failed for n in f["detail"]["number_ids"]]
        raise CheckFailed(f"{month} differs from the answer key in {len(numbers)} number(s): "
                          + ", ".join(numbers[:5]) + (" ..." if len(numbers) > 5 else ""), failed)
    missing = provenance_differences(con, month)
    if missing:
        raise ProvenanceMissing(f"{len(missing)} number(s) of {month} and earlier months have no matching "
                                f"provenance record: " + ", ".join(d["number_id"] for d in missing[:5])
                                + (" ..." if len(missing) > 5 else ""), [_failed(PROV_ID, missing)])
    return [controls.prov_control(con, month).summary()] + [_passed(i) for i in (ROLLFORWARD_ID, IDENTITIES_ID, PROV_ID)]
