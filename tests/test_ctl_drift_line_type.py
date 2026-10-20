"""CTL-DRIFT-LINE-TYPE: a contract line whose type no billing rule bills stops the close before any model
is built. The clean `mini` passes; a line changed to `services` fails with its contract, version and line,
until the line is put back to a billed type."""

from __future__ import annotations

import shutil

from c2c import db, dbt_runner

from conftest import MINI_BOOKS, MINI_WINDOW, c2c
from forecast_helpers import mini_closed  # noqa: F401  (session fixture)

CONTROL = "CTL-DRIFT-LINE-TYPE"
MONTH = "2026-01"


def planted_line(books) -> tuple[str, str, str]:
    before = (MINI_BOOKS / "contract_lines.csv").read_text(encoding="utf-8").splitlines()
    after = (books / "contract_lines.csv").read_text(encoding="utf-8").splitlines()
    (changed,) = [line for old, line in zip(before, after) if old != line]
    contract_id, version, line_id, product = changed.split(",")[:4]
    assert product == "services"
    return contract_id, version, line_id


def inject(tmp_path):
    out = tmp_path / "books"
    done = c2c("inject", "--error", "unbilled_line_type", "--month", MONTH, "--books", str(MINI_BOOKS), "--out", str(out))
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


def test_a_contract_line_of_an_unbilled_type_stops_the_close_before_dbt(tmp_path):
    books = inject(tmp_path)
    contract_id, version, line_id = planted_line(books)
    trace = tmp_path / "dbt-trace.txt"

    done = close(books, tmp_path, trace=trace)

    assert done.code == 3, done.stderr
    failed = {c["id"]: c for c in done.json["failed_controls"]}
    assert failed[CONTROL]["violating_rows"] == 1
    (remediation,) = failed[CONTROL]["remediation"]
    assert f"Contract {contract_id} version {version} line {line_id} has line type services" in remediation
    assert "subscription, overage" in remediation and "{" not in remediation
    assert len(failed[CONTROL]["detail"]["row_hashes"]) == 1
    assert not trace.exists(), "dbt must not start when a books control fails"


def test_putting_the_line_back_to_a_billed_type_passes(tmp_path):
    books = inject(tmp_path)
    fixed = tmp_path / "fixed"
    shutil.copytree(books, fixed)
    _, _, line_id = planted_line(books)
    path = fixed / "contract_lines.csv"
    path.write_text(path.read_text(encoding="utf-8").replace(f"{line_id},services,", f"{line_id},subscription,"), encoding="utf-8")

    done = close(fixed, tmp_path)

    assert done.code == 0, done.stderr
    assert any(c["id"] == CONTROL and c["status"] == "pass" for c in done.json["controls"])
