"""The books-integrity controls run on the loaded books before any model is built: a re-imported
invoice line, a repeated payment application and a dangling reference each exit 3 naming the row,
and dbt does not start."""

from __future__ import annotations

import csv
from pathlib import Path

import pytest

from c2c import dbt_runner

MONTH = "2026-01"
RAW_CONTROLS = ("CTL-DUP-INVOICE", "CTL-DUP-PAYMENT", "CTL-REF-INTEGRITY")


def read(books: Path, name: str) -> list[dict]:
    with (books / name).open(encoding="utf-8", newline="") as fh:
        return list(csv.DictReader(fh))


def write(books: Path, name: str, rows: list[dict]) -> None:
    with (books / name).open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def close(workspace, tmp_path):
    """Close January with a trace of the dbt subprocesses; returns the result and the trace."""
    trace = tmp_path / "dbt-trace.txt"
    done = workspace.run("close", "--month", MONTH, "--json", env={dbt_runner.TRACE_VAR: str(trace)})
    return done, trace


def assert_stopped_before_dbt(workspace, trace: Path) -> None:
    assert not trace.exists(), "dbt must not start when a books control fails"
    assert not (dbt_runner.scratch_dir(workspace.warehouse) / "target" / "run_results.json").exists()


def failed_by_id(done) -> dict:
    return {entry["id"]: entry for entry in done.json["failed_controls"]}


def test_the_clean_books_pass_all_three_controls_and_dbt_runs_once(mini_books_copy, workspace, tmp_path):
    done, trace = close(workspace, tmp_path)
    assert done.code == 0, done.stderr
    controls = {entry["id"]: entry for entry in done.json["controls"]}
    for control in RAW_CONTROLS:
        assert controls[control] == {"id": control, "status": "pass", "violating_rows": 0}
    assert len(trace.read_text(encoding="utf-8").splitlines()) == 1


def test_a_re_imported_invoice_line_fails_the_close_naming_it(mini_books_copy, workspace, tmp_path):
    invoices = read(mini_books_copy, "invoices.csv")
    first = invoices[0]
    write(mini_books_copy, "invoices.csv", invoices + [dict(first)])

    done, trace = close(workspace, tmp_path)

    assert done.code == 3, done.stderr
    failed = failed_by_id(done)
    assert set(failed) == {"CTL-DUP-INVOICE"}
    assert failed["CTL-DUP-INVOICE"]["violating_rows"] == 1
    [remediation] = failed["CTL-DUP-INVOICE"]["remediation"]
    assert first["invoice_id"] in remediation and f"line {first['line_no']}" in remediation
    assert "invoices.csv" in remediation and "{" not in remediation
    assert len(failed["CTL-DUP-INVOICE"]["detail"]["row_hashes"]) == 1  # both copies are the same row
    assert_stopped_before_dbt(workspace, trace)


def test_a_payment_applied_twice_to_one_invoice_fails_the_close_naming_it(mini_books_copy, workspace, tmp_path):
    payments = read(mini_books_copy, "payments.csv")
    first = payments[0]
    write(mini_books_copy, "payments.csv", payments + [dict(first)])

    done, trace = close(workspace, tmp_path)

    assert done.code == 3, done.stderr
    failed = failed_by_id(done)
    assert set(failed) == {"CTL-DUP-PAYMENT"}
    [remediation] = failed["CTL-DUP-PAYMENT"]["remediation"]
    assert first["payment_id"] in remediation and first["invoice_id"] in remediation
    assert "payments.csv" in remediation and "{" not in remediation
    assert_stopped_before_dbt(workspace, trace)


def test_one_payment_split_over_two_invoices_is_not_a_duplicate(mini_books_copy, workspace, tmp_path):
    payments = read(mini_books_copy, "payments.csv")
    other = next(row["invoice_id"] for row in read(mini_books_copy, "invoices.csv")
                 if row["invoice_id"] != payments[0]["invoice_id"])
    write(mini_books_copy, "payments.csv", payments + [dict(payments[0], invoice_id=other)])
    done, _ = close(workspace, tmp_path)
    assert "CTL-DUP-PAYMENT" not in {e["id"] for e in done.json.get("failed_controls", [])}


def test_a_dangling_contract_reference_fails_the_close_naming_the_row(mini_books_copy, workspace, tmp_path):
    invoices = read(mini_books_copy, "invoices.csv")
    first = invoices[0]
    first["contract_id"] = "ctr_9999_00"
    write(mini_books_copy, "invoices.csv", invoices)

    done, trace = close(workspace, tmp_path)

    assert done.code == 3, done.stderr
    failed = failed_by_id(done)
    assert set(failed) == {"CTL-REF-INTEGRITY"}
    [remediation] = failed["CTL-REF-INTEGRITY"]["remediation"]
    assert f"{first['invoice_id']}/{first['line_no']}" in remediation
    assert "contract ctr_9999_00" in remediation and "{" not in remediation
    assert_stopped_before_dbt(workspace, trace)


@pytest.mark.parametrize("file, column, kind", [
    ("payments.csv", "bank_txn_id", "bank transaction"),
    ("payments.csv", "invoice_id", "invoice"),
    ("payments.csv", "billing_customer_id", "billing customer"),
    ("invoices.csv", "contract_line_id", "contract line"),
    ("invoices.csv", "billing_customer_id", "billing customer"),
    ("contract_lines.csv", "version", "contract version"),
])
def test_each_kind_of_dangling_reference_is_reported_with_its_kind(mini_books_copy, workspace, tmp_path,
                                                                    file, column, kind):
    rows = read(mini_books_copy, file)
    rows[0][column] = "9999"
    write(mini_books_copy, file, rows)

    done, trace = close(workspace, tmp_path)

    assert done.code == 3, done.stderr
    remediation = failed_by_id(done)["CTL-REF-INTEGRITY"]["remediation"]
    assert any(f"{kind} " in step and "9999" in step for step in remediation), remediation
    assert_stopped_before_dbt(workspace, trace)
