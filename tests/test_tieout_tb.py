"""The general-ledger tie-out with a stacked trial balance: without one the trial balance columns are
empty, and with one built from the ledger itself every tied account's trial balance figure ties to its
sub-ledger in every month."""

from __future__ import annotations

import csv
from calendar import month_name, monthrange
from collections import defaultdict
from pathlib import Path

from c2c import db

from conftest import MINI_BOOKS, MINI_WINDOW, c2c
from forecast_helpers import mini_closed  # noqa: F401  (fixture)

TIED = ["1000", "1200", "2400", "4000", "4100"]


def query(warehouse: Path, sql: str, params=()):
    with db.connect(warehouse, read_only=True) as con:
        return con.execute(sql, params).fetchall()


def month_end(period: str) -> str:
    year, month = map(int, period.split("-"))
    return f"{month_name[month]} {monthrange(year, month)[1]}, {year}"


def trial_balance_files(directory: Path) -> None:
    """One QuickBooks Online trial balance export per month of the mini ledger. Every account carries
    its debits and credits from the first month to the period's end (a balance for 1000..3999, the
    year-to-date figure for 4000 and up, the books' first month being the start of the year), so the
    first period's net is the period's activity."""
    with open(MINI_BOOKS / "gl.csv", newline="", encoding="utf-8") as fh:
        lines = list(csv.DictReader(fh))
    activity: dict[str, dict[str, list[int]]] = defaultdict(lambda: defaultdict(lambda: [0, 0]))
    for line in lines:
        cents = int(line["amount_cents"])
        total = activity[line["gl_period"]][line["gl_account"]]
        total[0] += max(cents, 0)
        total[1] += max(-cents, 0)
    directory.mkdir()
    running: dict[str, list[int]] = {line["gl_account"]: [0, 0] for line in lines}
    for period in sorted(activity):
        for account, (debit, credit) in activity[period].items():
            running[account][0] += debit
            running[account][1] += credit
        # every account of the books is listed from the first month on, so none is a chart change
        rows = [[f"{account} Account {account}", f"{d / 100:.2f}" if d else "", f"{c / 100:.2f}" if c else ""]
                for account, (d, c) in sorted(running.items())]
        total_d = sum(d for d, _ in running.values())
        total_c = sum(c for _, c in running.values())
        with open(directory / f"trial_balance_{period}.csv", "w", newline="", encoding="utf-8") as fh:
            out = csv.writer(fh, lineterminator="\n")
            out.writerow(["Synthetic Co (synthetic)"])
            out.writerow(["Trial Balance"])
            out.writerow([f"As of {month_end(period)}"])
            out.writerow([])
            out.writerow(["", "Debit", "Credit"])
            out.writerows(rows)
            out.writerow(["TOTAL", f"{total_d / 100:.2f}", f"{total_c / 100:.2f}"])


def test_without_a_trial_balance_the_trial_balance_columns_are_empty(mini_closed):
    rows = query(mini_closed, "SELECT count(*), count(tb_cents), count(tb_diff_cents) FROM mart.fct_gl_tieout")
    assert rows[0][0] > 0 and rows[0][1:] == (0, 0)


def test_with_a_trial_balance_built_from_the_ledger_every_tied_account_ties(tmp_path):
    directory = tmp_path / "tb"
    trial_balance_files(directory)
    warehouse = tmp_path / "w.duckdb"
    loaded = c2c("load-tb", str(directory), "--warehouse", str(warehouse), "--csv", str(tmp_path / "tb.csv"),
                 "--json")
    assert loaded.code == 0, loaded.stderr
    done = c2c("close", "--month", "2026-03", "--books", str(MINI_BOOKS), "--warehouse", str(warehouse),
               "--json")
    assert done.code == 0, done.stderr

    rows = query(warehouse, "SELECT month, gl_account, tb_cents, tb_diff_cents, subledger_cents"
                 " FROM mart.fct_gl_tieout ORDER BY month, gl_account")
    assert {account for _, account, *_ in rows} == set(TIED)
    assert len({month for month, *_ in rows}) >= 18
    assert all(diff == 0 for _, _, _, diff, _ in rows)
    assert all(tb == sub for _, _, tb, _, sub in rows)
    assert any(tb != 0 for _, _, tb, _, _ in rows)
    assert query(warehouse, "SELECT count(*) FROM mart.fct_gl_tieout WHERE diff_cents != 0") == [(0,)]

    control = query(warehouse, "SELECT status FROM ctl.control_results WHERE control_id = 'CTL-TIE-AR'"
                    " AND month = ?", [MINI_WINDOW[1]])
    assert control and all(status == "pass" for (status,) in control)
