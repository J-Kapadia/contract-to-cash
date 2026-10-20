"""The one-time items of a month: credit memos, write-offs and manual adjustments. The `mini` books carry
none, so a closed month holds an empty mart and three zero totals; planted rows show each category."""

from __future__ import annotations

import csv
from pathlib import Path

from c2c import db, prov

from conftest import MINI_BOOKS, MINI_WINDOW, c2c
from forecast_helpers import mini_closed  # noqa: F401  (fixture)

MONTH = "2026-03"
TOTALS = ("credit_memos", "write_offs", "manual_adjustments")
CREDIT_LINE = {"invoice_id": "inv_000100", "line_no": "1", "billing_customer_id": "bill_0002",
               "contract_id": "ctr_0002_00", "contract_line_id": "ctr_0002_00_L1", "invoice_date": "2026-03-15",
               "gl_period": MONTH, "due_date": "2026-03-15", "service_start": "2026-03-15",
               "service_end": "2026-03-15", "line_type": "credit", "amount_cents": "-500000",
               "known_on": "2026-03-15"}


def months(first: str, last: str) -> list[str]:
    out, (year, month) = [], map(int, first.split("-"))
    while f"{year}-{month:02d}" <= last:
        out.append(f"{year}-{month:02d}")
        year, month = (year + 1, 1) if month == 12 else (year, month + 1)
    return out


def read(books: Path, name: str) -> list[dict]:
    with (books / name).open(encoding="utf-8", newline="") as fh:
        return list(csv.DictReader(fh))


def write(books: Path, name: str, rows: list[dict]) -> None:
    with (books / name).open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def ledger(entry: str, posted: str, lines: list[tuple[str, int, str, str]]) -> list[dict]:
    return [{"entry_id": entry, "line_no": str(n), "posted_on": posted, "gl_period": posted[:7],
             "gl_account": account, "amount_cents": str(cents), "subledger_name": name, "source_ref": ref,
             "known_on": posted} for n, (account, cents, name, ref) in enumerate(lines, 1)]


def test_a_closed_mini_has_no_item_and_three_zero_totals(mini_closed):
    with db.connect(mini_closed, read_only=True) as con:
        assert con.execute("SELECT count(*) FROM mart.fct_onetime_items").fetchall() == [(0,)]
        stored = dict(con.execute(
            "SELECT number_id, value_cents FROM prov.records WHERE number_id LIKE 'onetime/%'").fetchall())
    closed = months(*MINI_WINDOW)
    assert stored == {f"onetime/{month}/{total}": 0 for month in closed for total in TOTALS}

    done = c2c("explain", f"onetime/{MINI_WINDOW[1]}/credit_memos", "--json", "--books", str(MINI_BOOKS),
               "--warehouse", str(mini_closed))
    assert done.code == 0, done.stderr


def test_planted_items_are_listed_and_registered(mini_books_copy, workspace):
    write(mini_books_copy, "invoices.csv", read(mini_books_copy, "invoices.csv") + [CREDIT_LINE])
    memo = ledger("je_900001", "2026-03-15", [("4000", 500000, "", "inv_000100"),
                                              ("1200", -500000, "Briar Mobility Corp", "inv_000100")])
    write_off = ledger("je_900002", "2026-03-20",
                       [("6300", 250000, "", "wo_000001"), ("1200", -250000, "", "wo_000001")])
    manual = ledger("je_900003", "2026-03-21", [("6400", 100000, "", ""), ("3000", -100000, "", "")])
    write(mini_books_copy, "gl.csv", read(mini_books_copy, "gl.csv") + memo + write_off + manual)

    # (inv_000100: the mini books already hold an inv_000099, and a repeated invoice line stops the close.)
    # The write-off moves GL 1200 away from the subledger, so a tie-out may stop the close; the models are
    # built before the controls of the mart stage run.
    workspace.run("close", "--month", MONTH, "--json")

    with db.connect(workspace.warehouse, read_only=True) as con:
        items = con.execute("SELECT month, category, ref_id, amount_cents FROM mart.fct_onetime_items"
                            " ORDER BY category").fetchall()
        assert items == [(MONTH, "credit_memo", "inv_000100/1", -500000),
                         (MONTH, "manual_adjustment", "je_900003", 100000),
                         (MONTH, "write_off", "je_900002/2", -250000)]
        rows = {r["number_id"]: r for r in prov.family_rows(con, prov.FAMILIES_DIR / "onetime.sql", MONTH)}
        assert {number_id: row["value_cents"] for number_id, row in rows.items()} == {
            f"onetime/{MONTH}/credit_memos": -500000,
            f"onetime/{MONTH}/write_offs": -250000,
            f"onetime/{MONTH}/manual_adjustments": 100000,
            f"onetime/{MONTH}/credit_memo/inv_000100/1": -500000,
            f"onetime/{MONTH}/write_off/je_900002/2": -250000,
            f"onetime/{MONTH}/manual_adjustment/je_900003": 100000}
        assert {row["unit"] for row in rows.values()} == {"cents"}
        for number_id, row in rows.items():
            hashes = con.execute(row["source_query"]).fetchall()
            assert len(hashes) == (2 if "manual_adjustment" in number_id else 1), number_id
