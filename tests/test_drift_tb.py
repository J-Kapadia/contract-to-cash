"""What changed in the books with a stacked trial balance in the warehouse: an account that first
appears in a trial balance period and that the raw ledger does not hold is a first sighting too."""

from __future__ import annotations

import csv
import shutil
from collections import defaultdict
from pathlib import Path

from c2c import db

from conftest import MINI_BOOKS, MINI_WINDOW, c2c
from forecast_helpers import mini_closed  # noqa: F401  (fixture)
from test_drift_numbers import MONTH, drift_rows, expected_rows, query

NEW_MONTH, NEW_ACCOUNT = "2025-09", "6900"


def month_end(period: str) -> str:
    from calendar import month_name, monthrange

    year, month = map(int, period.split("-"))
    return f"{month_name[month]} {monthrange(year, month)[1]}, {year}"


def trial_balance_files(directory: Path) -> None:
    """One export per month of the mini ledger, each month's activity by account, plus account 6900
    from 2025-09 on with a one-cent debit and credit against cash."""
    with open(MINI_BOOKS / "gl.csv", newline="", encoding="utf-8") as fh:
        lines = list(csv.DictReader(fh))
    activity: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for line in lines:
        activity[line["gl_period"]][line["gl_account"]] += int(line["amount_cents"])
    names = {"1000": "Cash"}
    directory.mkdir()
    for period in sorted(activity):
        net = dict(activity[period])
        if period >= NEW_MONTH:
            net["1000"] = net.get("1000", 0)
            debit = {NEW_ACCOUNT: 1}
            credit = {"1000": 1}
        else:
            debit, credit = {}, {}
        rows = []
        for account in sorted(set(net) | set(debit)):
            cents = net.get(account, 0)
            d = max(cents, 0) + debit.get(account, 0)
            c = max(-cents, 0) + credit.get(account, 0)
            name = names.get(account, f"Account {account}") if account != NEW_ACCOUNT else "Bad debt"
            rows.append([f"{account} {name}", f"{d / 100:.2f}" if d else "", f"{c / 100:.2f}" if c else ""])
        total_d = sum(int(round(float(r[1]) * 100)) for r in rows if r[1])
        total_c = sum(int(round(float(r[2]) * 100)) for r in rows if r[2])
        with open(directory / f"trial_balance_{period}.csv", "w", newline="", encoding="utf-8") as fh:
            out = csv.writer(fh, lineterminator="\n")
            out.writerow(["Synthetic Co (synthetic)"])
            out.writerow(["Trial Balance"])
            out.writerow([f"As of {month_end(period)}"])
            out.writerow([])
            out.writerow(["", "Debit", "Credit"])
            out.writerows(rows)
            out.writerow(["TOTAL", f"{total_d / 100:.2f}", f"{total_c / 100:.2f}"])


def test_without_a_trial_balance_the_drift_rows_are_the_ones_the_csvs_hold(mini_closed):
    assert drift_rows(mini_closed, MONTH) == expected_rows(MONTH)
    with db.connect(mini_closed, read_only=True) as con:
        assert con.execute("select count(*) from information_schema.tables"
                           " where table_name = 'fct_trial_balance'").fetchone()[0] == 0
    keys = {k for (k,) in query(mini_closed, "SELECT key FROM mart.fct_drift WHERE category = 'new_account'")}
    ledger = {k for (k,) in query(mini_closed, "SELECT DISTINCT gl_account FROM raw.gl")}
    assert keys <= ledger


def test_an_account_only_the_trial_balance_holds_is_new_in_its_first_period(tmp_path):
    directory = tmp_path / "tb"
    trial_balance_files(directory)
    warehouse = tmp_path / "w.duckdb"
    loaded = c2c("load-tb", str(directory), "--warehouse", str(warehouse), "--csv", str(tmp_path / "tb.csv"),
                 "--json")
    assert loaded.code in (0, 3), loaded.stderr  # 3: the new account is also flagged by the trial balance controls
    done = c2c("close", "--month", MINI_WINDOW[1], "--books", str(MINI_BOOKS), "--warehouse", str(warehouse),
               "--json")
    assert done.code == 0, done.stderr

    rows = query(warehouse, "SELECT value_cents, prior_cents, shift_bps FROM mart.fct_drift"
                 " WHERE month = ? AND category = 'new_account' AND key = ?", [NEW_MONTH, NEW_ACCOUNT])
    assert rows == [(1, None, None)]
    assert query(warehouse, "SELECT count(*) FROM mart.fct_drift WHERE key = ?", [NEW_ACCOUNT]) == [(1,)]
    # the ledger's own accounts are not new in the trial balance's first period
    assert drift_rows(warehouse, MONTH) == expected_rows(MONTH)

    number = f"drift/{NEW_MONTH}/new_account/{NEW_ACCOUNT}"
    ((value, step, source),) = query(warehouse, "SELECT value_cents, step, source_query FROM prov.records"
                                     " WHERE number_id = ?", [number])
    assert value == 1 and step == "fct_drift"
    assert "mart.fct_trial_balance" in source and NEW_ACCOUNT in source

    (count,) = query(warehouse, "SELECT value_cents FROM prov.records WHERE number_id = ?",
                     [f"drift/{NEW_MONTH}/new_accounts"])[0]
    (items,) = query(warehouse, "SELECT count(*) FROM mart.fct_drift WHERE month = ? AND category = 'new_account'",
                     [NEW_MONTH])[0]
    assert count == items and count >= 1

    copy = tmp_path / "copy.duckdb"
    shutil.copyfile(warehouse, copy)
    explained = c2c("explain", number, "--json", "--books", str(MINI_BOOKS), "--warehouse", str(copy))
    assert explained.code == 0, explained.stderr
    assert explained.json["explanation"]["digest_matches"] is True
