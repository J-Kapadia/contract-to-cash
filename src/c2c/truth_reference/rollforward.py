"""The expected roll-forward and revenue-to-cash bridge files of a set of books.

`rollforward.csv` has one row per customer and month: recognised revenue and the deferred
revenue and receivable roll-forwards (opening, additions, reductions, closing). `bridge.csv`
has one row per month: the five lines of the revenue-to-cash bridge. Both come from the
package's arithmetic alone, in whole cents, never from the warehouse.
"""

from __future__ import annotations

import csv
import io
import json
from collections import defaultdict
from pathlib import Path

from c2c import truth_reference as tr

ROLLFORWARD_COLUMNS = (
    "customer_id", "month", "revenue_cents",
    "defrev_opening_cents", "defrev_additions_cents", "defrev_reductions_cents", "defrev_closing_cents",
    "ar_opening_cents", "ar_additions_cents", "ar_reductions_cents", "ar_closing_cents",
)
BRIDGE_COLUMNS = ("month", *tr.BRIDGE_LINES)


def read(path: Path) -> list[dict]:
    with path.open(encoding="utf-8", newline="") as fh:
        return list(csv.DictReader(fh))


def render(columns: tuple[str, ...], rows: list[dict]) -> str:
    out = io.StringIO()
    writer = csv.writer(out, lineterminator="\n")
    writer.writerow(columns)
    writer.writerows([row[c] for c in columns] for row in rows)
    return out.getvalue()


def rollforwards(books: Path, truth: Path) -> tr.RollForwards:
    """Roll-forwards per customer (the CRM account behind each billing customer) through the books' last month."""
    customer_of = {r["source_id"]: r["true_customer_id"]
                   for r in read(truth / "identities.csv") if r["source_system"] == "billing"}
    window = json.loads((books / "window.json").read_text(encoding="utf-8"))
    invoices = [{**r, "customer_id": customer_of[r["billing_customer_id"]]} for r in read(books / "invoices.csv")]
    payments = [{**r, "customer_id": customer_of[r["billing_customer_id"]]} for r in read(books / "payments.csv")]
    return tr.rollforward(invoices, payments, through=window["last_month"], customer_key="customer_id")


def rollforward_rows(rolls: tr.RollForwards) -> list[dict]:
    revenue: dict[tuple[str, str], int] = defaultdict(int)
    for (customer, month, _), cents in rolls.revenue.items():
        revenue[customer, month] += cents
    rows = []
    for (customer, month), defrev in sorted(rolls.defrev.items()):
        ar = rolls.ar[customer, month]
        rows.append({
            "customer_id": customer, "month": month, "revenue_cents": revenue[customer, month],
            "defrev_opening_cents": defrev.opening, "defrev_additions_cents": defrev.additions,
            "defrev_reductions_cents": defrev.reductions, "defrev_closing_cents": defrev.closing,
            "ar_opening_cents": ar.opening, "ar_additions_cents": ar.additions,
            "ar_reductions_cents": ar.reductions, "ar_closing_cents": ar.closing,
        })
    return rows


def bridge_rows(rolls: tr.RollForwards) -> list[dict]:
    return [{"month": month, **tr.bridge(month, rolls)} for month in rolls.months]


def expected(books: Path, truth: Path, config: Path) -> dict[str, str]:
    """The text of each expected file, by file name. The roll-forwards read no configuration."""
    rolls = rollforwards(books, truth)
    return {"rollforward.csv": render(ROLLFORWARD_COLUMNS, rollforward_rows(rolls)),
            "bridge.csv": render(BRIDGE_COLUMNS, bridge_rows(rolls))}
