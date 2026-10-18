"""Provenance: one record per published number, with a digest of the source rows it came from.

A leaf number hashes the `_row_hash` of every source row its `source_query` returns. Records are
written only here.
"""

from __future__ import annotations

import functools
import hashlib
import os
import subprocess
import uuid
from collections.abc import Sequence
from pathlib import Path

import duckdb

UNKNOWN_SHA = "unknown"


def init(con: duckdb.DuckDBPyConnection) -> None:
    con.execute("CREATE SCHEMA IF NOT EXISTS prov")
    con.execute(
        "CREATE TABLE IF NOT EXISTS prov.records ("
        " number_id VARCHAR PRIMARY KEY, month VARCHAR, value_cents BIGINT, value_text VARCHAR, unit VARCHAR,"
        " step VARCHAR, git_sha VARCHAR, producer_kind VARCHAR, input_digest VARCHAR, input_row_count BIGINT,"
        " source_query VARCHAR, checks_passed VARCHAR[], signer VARCHAR, signoff_ref VARCHAR, run_id VARCHAR)"
    )


@functools.cache
def _head_sha() -> str:
    try:
        out = subprocess.run(["git", "rev-parse", "HEAD"], cwd=Path(__file__).resolve().parent,
                             capture_output=True, text=True, check=False)
    except OSError:
        return UNKNOWN_SHA
    return out.stdout.strip() if out.returncode == 0 and out.stdout.strip() else UNKNOWN_SHA


def git_sha() -> str:
    """The commit that produced a number: `C2C_GIT_SHA`, else `git rev-parse HEAD`, else "unknown"."""
    return os.environ.get("C2C_GIT_SHA") or _head_sha()


def producer_kind() -> str:
    return os.environ.get("C2C_PRODUCER_KIND") or "cli"


def digest(row_hashes: Sequence[str]) -> str:
    """SHA-256 over the sorted row hashes, one per line."""
    return hashlib.sha256("\n".join(sorted(row_hashes)).encode("utf-8")).hexdigest()


def source_digest(con: duckdb.DuckDBPyConnection, source_query: str) -> tuple[str, int]:
    """The input digest and row count of what `source_query` returns (it selects `_row_hash`)."""
    hashes = [row[0] for row in con.execute(source_query).fetchall()]
    return digest(hashes), len(hashes)


def write_leaf(con: duckdb.DuckDBPyConnection, number_id: str, month: str, value: int | str | None, unit: str,
               step: str, source_query: str, checks_passed: Sequence[str] = (), run_id: str | None = None) -> None:
    """Write (or replace) the record of one leaf number. An integer `value` is stored as cents."""
    init(con)
    input_digest, count = source_digest(con, source_query)
    is_text = isinstance(value, str)
    con.execute(
        "INSERT OR REPLACE INTO prov.records VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, ?)",
        [number_id, month, None if is_text else value, value if is_text else None, unit, step, git_sha(),
         producer_kind(), input_digest, count, source_query, list(checks_passed), run_id or uuid.uuid4().hex],
    )


def _quoted(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def ar_source_query(customer_id: str, month: str) -> str:
    """The invoice and payment rows that make up a customer's receivable through `month`."""
    customer, through = _quoted(customer_id), _quoted(month)
    return (
        "WITH owners AS (SELECT contract_id, min(crm_account_id) AS customer_id FROM raw.contracts GROUP BY contract_id),"
        " invoice_owners AS (SELECT invoice_id, min(contract_id) AS contract_id FROM raw.invoices GROUP BY invoice_id)"
        f" SELECT invoices._row_hash FROM raw.invoices AS invoices JOIN owners USING (contract_id)"
        f" WHERE owners.customer_id = {customer} AND invoices._month <= {through}"
        f" UNION ALL SELECT payments._row_hash FROM raw.payments AS payments"
        f" JOIN invoice_owners USING (invoice_id) JOIN owners USING (contract_id)"
        f" WHERE owners.customer_id = {customer} AND payments._month <= {through}"
    )


def write_ar_closing(con: duckdb.DuckDBPyConnection, month: str, checks_passed: Sequence[str] = (),
                     run_id: str | None = None) -> int:
    """Write `ar/<month>/<customer_id>/closing` for every customer of the AR roll-forward; returns the count."""
    run_id = run_id or uuid.uuid4().hex
    rows = con.execute("SELECT customer_id, closing_cents FROM mart.fct_ar_rollforward"
                       " WHERE month = ? AND customer_id IS NOT NULL ORDER BY customer_id", [month]).fetchall()
    con.execute("BEGIN")
    try:
        for customer_id, closing in rows:
            write_leaf(con, f"ar/{month}/{customer_id}/closing", month, closing, "cents", "fct_ar_rollforward",
                       ar_source_query(customer_id, month), checks_passed, run_id)
        con.execute("COMMIT")
    except BaseException:
        con.execute("ROLLBACK")
        raise
    return len(rows)
