"""CTL-CUTOFF and CTL-BILL-CONTRACT: each passes on the clean `mini` books and fails the close, naming the
violating row in its remediation, on a copy of the books with the error planted."""

from __future__ import annotations

import csv
import re
from pathlib import Path

from conftest import c2c

MONTH = "2026-01"
MOVED_INVOICE = "inv_000070"  # a December 2025 invoice
AMENDED_CONTRACT, AMENDED_LINE = "ctr_0002_00", "ctr_0002_00_L1"  # annual, term 2024-11-01 to 2026-10-31
AMENDMENT_EFFECTIVE = "2026-01-15"


def read(books: Path, name: str) -> list[dict]:
    with (books / name).open(encoding="utf-8", newline="") as fh:
        return list(csv.DictReader(fh))


def write(books: Path, name: str, rows: list[dict]) -> None:
    with (books / name).open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def close_january(workspace) -> tuple:
    done = workspace.run("close", "--month", MONTH, "--json")
    return done, {entry["id"]: entry for entry in done.json.get("failed_controls", [])}


def test_the_clean_mini_passes_both_controls(mini_books_copy, workspace):
    done = c2c("close", "--month", MONTH, "--books", str(mini_books_copy), "--warehouse", str(workspace.warehouse),
               "--json")
    assert done.code == 0, done.stderr
    controls = {entry["id"]: entry for entry in done.json["controls"]}
    assert controls["CTL-CUTOFF"] == {"id": "CTL-CUTOFF", "status": "pass", "violating_rows": 0}
    assert controls["CTL-BILL-CONTRACT"] == {"id": "CTL-BILL-CONTRACT", "status": "pass", "violating_rows": 0}


def test_an_invoice_posted_to_the_next_period_fails_the_close_naming_it(mini_books_copy, workspace):
    invoices = {row["invoice_id"]: row for row in read(mini_books_copy, "invoices.csv")}
    assert invoices[MOVED_INVOICE]["invoice_date"].startswith("2025-12")
    gl = read(mini_books_copy, "gl.csv")
    moved = [row for row in gl if row["source_ref"] == MOVED_INVOICE]
    assert moved, "the invoice has GL entries"
    for row in moved:
        row["gl_period"] = MONTH
    write(mini_books_copy, "gl.csv", gl)

    done, failed = close_january(workspace)

    assert done.code == 3, done.stderr
    cutoff = failed["CTL-CUTOFF"]
    assert cutoff["violating_rows"] == 1
    [remediation] = cutoff["remediation"]
    assert MOVED_INVOICE in remediation and "2025-12" in remediation and "{" not in remediation
    assert len(cutoff["detail"]["row_hashes"]) == len(moved)
    assert cutoff["detail"]["number_ids"], "the revenue and deferred revenue of the customer-month are named"
    assert all(n.startswith(("rev/2025-12/", "defrev/2025-12/")) for n in cutoff["detail"]["number_ids"])
    assert "CTL-BILL-CONTRACT" not in failed


def test_an_amendment_without_its_prorated_invoice_fails_the_close_naming_it(mini_books_copy, workspace):
    contracts = read(mini_books_copy, "contracts.csv")
    [first] = [row for row in contracts if row["contract_id"] == AMENDED_CONTRACT]
    amendment = dict(first, version="2", effective_from=AMENDMENT_EFFECTIVE, signed_on="2026-01-10",
                     known_on="2026-01-10")
    write(mini_books_copy, "contracts.csv", contracts + [amendment])
    lines = read(mini_books_copy, "contract_lines.csv")
    [line] = [row for row in lines if row["line_id"] == AMENDED_LINE]
    raised = dict(line, version="2", annual_fee_cents=str(int(line["annual_fee_cents"]) + 3_650_000),
                  known_on="2026-01-10")
    write(mini_books_copy, "contract_lines.csv", lines + [raised])

    done, failed = close_january(workspace)

    assert done.code == 3, done.stderr
    billing = failed["CTL-BILL-CONTRACT"]
    assert billing["violating_rows"] == 1
    [remediation] = billing["remediation"]
    assert AMENDED_CONTRACT in remediation and AMENDED_LINE in remediation
    assert f"service {AMENDMENT_EFFECTIVE} to 2026-10-31" in remediation
    assert "{" not in remediation and re.search(r"\b[1-9]\d* cents", remediation), remediation
    assert "CTL-CUTOFF" not in failed
