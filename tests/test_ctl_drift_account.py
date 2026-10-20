"""CTL-DRIFT-ACCOUNT: a ledger account the reference does not map stops the close before any model is
built. The clean `mini` passes; an expense line moved to an unlisted account fails with its account,
until the account is added to the reference file."""

from __future__ import annotations

import shutil

from c2c import db, dbt_runner

from conftest import MINI_BOOKS, MINI_WINDOW, ROOT, c2c
from forecast_helpers import mini_closed  # noqa: F401  (session fixture)

CONTROL = "CTL-DRIFT-ACCOUNT"
MONTH = "2026-01"
REFERENCE_FILE = "data/reference/gl_accounts.csv"


def planted_account(books) -> str:
    before = (MINI_BOOKS / "gl.csv").read_text(encoding="utf-8").splitlines()
    after = (books / "gl.csv").read_text(encoding="utf-8").splitlines()
    (changed,) = [line for old, line in zip(before, after) if old != line]
    return changed.split(",")[4]


def inject(tmp_path):
    out = tmp_path / "books"
    done = c2c("inject", "--error", "unmapped_account", "--month", MONTH, "--books", str(MINI_BOOKS), "--out", str(out))
    assert done.code == 0, done.stderr
    return out


def close(books, tmp_path, *extra, trace=None):
    args = ["close", "--month", MONTH, "--fixture", "mini", "--books", str(books),
            "--warehouse", str(tmp_path / "w.duckdb"), "--json", *extra]
    env = {"C2C_BOOKS": ""}
    if trace is not None:
        env[dbt_runner.TRACE_VAR] = str(trace)
    return c2c(*args, env=env)


def test_a_clean_mini_has_a_pass_row_for_the_closed_month(mini_closed):  # noqa: F811
    with db.connect(mini_closed, read_only=True) as con:
        rows = con.execute("SELECT status, violating_rows FROM ctl.control_results WHERE control_id = ? AND month = ?",
                           [CONTROL, MINI_WINDOW[1]]).fetchall()
    assert rows == [("pass", 0)]


def test_a_ledger_line_on_an_unmapped_account_stops_the_close_before_dbt(tmp_path):
    books = inject(tmp_path)
    account = planted_account(books)
    trace = tmp_path / "dbt-trace.txt"

    done = close(books, tmp_path, trace=trace)

    assert done.code == 3, done.stderr
    failed = {c["id"]: c for c in done.json["failed_controls"]}
    assert failed[CONTROL]["violating_rows"] == 1
    (remediation,) = failed[CONTROL]["remediation"]
    assert account in remediation and REFERENCE_FILE in remediation and "{" not in remediation
    assert len(failed[CONTROL]["detail"]["row_hashes"]) == 1
    assert not trace.exists(), "dbt must not start when a books control fails"


def test_adding_the_account_to_the_reference_passes(tmp_path):
    books = inject(tmp_path)
    reference = tmp_path / "reference"
    shutil.copytree(ROOT / "data" / "reference", reference)
    with open(reference / "gl_accounts.csv", "a", encoding="utf-8", newline="") as fh:
        fh.write(f"{planted_account(books)},Planted account,expense\n")

    done = close(books, tmp_path, "--reference", str(reference))

    assert done.code == 0, done.stderr
    assert any(c["id"] == CONTROL and c["status"] == "pass" for c in done.json["controls"])
