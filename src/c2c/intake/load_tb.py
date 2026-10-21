"""A directory of monthly trial balance exports stacked into one mart, `mart.fct_trial_balance`, and
one CSV, then checked by the controls of the `tb` stage.

Choices made where the brief is silent: the row hash covers the four text columns (period, account id,
account name, source file); the envelope's `files` is the number of files read.
"""

from __future__ import annotations

import uuid
from pathlib import Path

import duckdb

from .. import controls, db, paths
from ..envelope import Envelope
from ..errors import UsageError
from . import trial_balance
from .trial_balance import TrialBalance

STAGE = "tb"
CSV_NAME = "fct_trial_balance.csv"

_COLUMNS = ("period, account_id, account_name, debit_cents, credit_cents, net_cents, source_file, _row_hash")


def _quoted(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def stack(directory: str | Path) -> list[TrialBalance]:
    """Every `*.csv` and `*.xlsx` directly in `directory`, in name order, parsed; two files for one
    period raise `UsageError` naming both, before anything is written."""
    directory = Path(directory)
    if not directory.is_dir():
        raise UsageError(f"{directory}: not a directory; pass the directory of monthly trial balance exports.")
    files = sorted((p for p in directory.iterdir() if p.is_file() and p.suffix.lower() in (".csv", ".xlsx")),
                   key=lambda p: p.name)
    if not files:
        raise UsageError(f"{directory}: no .csv or .xlsx trial balance files; add the monthly exports.")
    balances: list[TrialBalance] = []
    seen: dict[str, str] = {}
    for path in files:
        balance = trial_balance.parse(path)
        if balance.period in seen:
            raise UsageError(f"{seen[balance.period]} and {path.name} both cover {balance.period}; "
                             f"keep one export for {balance.period} and move the other out of {directory}.")
        seen[balance.period] = path.name
        balances.append(balance)
    return balances


def write(con: duckdb.DuckDBPyConnection, balances: list[TrialBalance], csv_path: str | Path) -> int:
    """Write `mart.fct_trial_balance`, `mart.fct_trial_balance_totals` and the CSV of the first; returns the row count."""
    csv_path = Path(csv_path)
    con.execute("CREATE OR REPLACE TEMP TABLE _tb_stage (period VARCHAR, account_id VARCHAR, account_name VARCHAR,"
                " debit_cents BIGINT, credit_cents BIGINT, source_file VARCHAR)")
    con.executemany("INSERT INTO _tb_stage VALUES (?, ?, ?, ?, ?, ?)",
                    [(b.period, r.account_id, r.account_name, r.debit_cents, r.credit_cents, r.source_file)
                     for b in balances for r in b.rows])
    con.execute("CREATE OR REPLACE TABLE mart.fct_trial_balance (period VARCHAR, account_id VARCHAR,"
                " account_name VARCHAR, debit_cents BIGINT, credit_cents BIGINT, net_cents BIGINT,"
                " source_file VARCHAR, _row_hash VARCHAR)")
    con.execute("INSERT INTO mart.fct_trial_balance SELECT period, account_id, account_name, debit_cents,"
                " credit_cents, debit_cents - credit_cents, source_file,"
                " sha256(concat_ws(chr(31), period, account_id, account_name, source_file))"
                " FROM _tb_stage ORDER BY period, account_id")
    con.execute("DROP TABLE _tb_stage")
    con.execute("CREATE OR REPLACE TABLE mart.fct_trial_balance_totals (period VARCHAR, total_debit_cents BIGINT,"
                " total_credit_cents BIGINT, source_file VARCHAR)")
    con.executemany("INSERT INTO mart.fct_trial_balance_totals VALUES (?, ?, ?, ?)",
                    [(b.period, b.total_debit_cents, b.total_credit_cents, b.rows[0].source_file if b.rows else None)
                     for b in balances if b.total_debit_cents is not None or b.total_credit_cents is not None])
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    con.execute(f"COPY (SELECT {_COLUMNS} FROM mart.fct_trial_balance ORDER BY period, account_id)"
                f" TO {_quoted(str(csv_path))} (HEADER)")
    return con.execute("SELECT count(*) FROM mart.fct_trial_balance").fetchone()[0]


def load(resolved: paths.Paths, directory: str | Path, csv_path: str | Path) -> Envelope:
    """Stack `directory` into the warehouse and the CSV, then run the `tb` controls on the result.

    A failed control raises `ControlFailure` (exit 3) with the table and the CSV left written."""
    balances = stack(directory)
    con = db.connect(resolved.warehouse)
    try:
        db.init_schemas(con)
        rows = write(con, balances, csv_path)
        periods = sorted(b.period for b in balances)
        results = controls.run(con, periods[-1], STAGE, uuid.uuid4().hex, directory=controls.CONTROLS_DIR / STAGE)
    finally:
        con.close()
    return Envelope(command="load-tb", status="ok", exit=0, month=periods[-1],
                    controls=[r.summary() for r in results],
                    extra={"periods": periods, "files": len(balances), "rows": rows, "csv": str(csv_path)},
                    message=f"loaded {rows} trial balance rows from {len(balances)} files "
                            f"({periods[0]} to {periods[-1]}), {len(results)} controls passed")
