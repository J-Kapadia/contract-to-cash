# The layouts were taken from observed exports. QuickBooks Online: a title block, a header row whose
# first cell is blank, one row per account named `<number> <name>`, a TOTAL row and a footer.
# NetSuite: the same, with `Account` in the first header cell. Neither vendor documents the export,
# so the parser reads by shape, not by label: the header row is the one with a Debit and a Credit
# cell, the data ends at a Total row or a blank first cell, and the rest is ignored. A layout change
# is caught by CTL-DRIFT-SCHEMA on the loaded books.
"""A monthly trial balance export read into one shape: period, account id, account name, debit and
credit in integer cents.

Choices made where the brief is silent: an account cell that is a number alone keeps the number as
its name; the number must be followed by a space or end the cell, so `3D printing` has no number.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from ..errors import UsageError
from . import report

_ACCOUNT = re.compile(r"^(?P<id>[\d.]+)(?:\s+(?P<name>.*))?$", re.DOTALL)


@dataclass(frozen=True)
class TrialBalanceRow:
    account_id: str
    account_name: str
    debit_cents: int
    credit_cents: int
    source_file: str


@dataclass(frozen=True)
class TrialBalance:
    period: str
    layout: str  # "qbo" or "netsuite": a label only, never used to parse
    rows: list[TrialBalanceRow]
    total_debit_cents: int | None
    total_credit_cents: int | None


def _account(cell: str) -> tuple[str, str]:
    """The account id and name of a `<number> <name>` cell."""
    text = cell.strip()
    m = _ACCOUNT.match(text)
    if m is None:
        return text, text
    name = (m["name"] or "").strip()
    return m["id"], name or text


def parse(path: str | Path) -> TrialBalance:
    path = Path(path)
    table = report.rows_of(path)
    head = report.header_index(table)
    if head is None:
        raise UsageError(f"{path}: no header row; a trial balance needs a header row with a Debit and a Credit cell")
    header = [cell.strip().lower() for cell in table[head]]
    debit, credit = header.index("debit"), header.index("credit")
    layout = "qbo" if not header[0] else "netsuite"
    period = report.period_of(path, table[:head])

    def amounts(row: list[str]) -> tuple[int, int]:
        try:
            return report.cents(row[debit]), report.cents(row[credit])
        except UsageError as exc:
            raise UsageError(f"{path}: {exc.message}") from exc

    rows: list[TrialBalanceRow] = []
    total_debit = total_credit = None
    for row in table[head + 1 :]:
        first = row[0].strip()
        if not first:
            break
        if first.lower().startswith("total"):
            total_debit, total_credit = amounts(row)
            break
        account_id, account_name = _account(first)
        rows.append(TrialBalanceRow(account_id, account_name, *amounts(row), path.name))
    return TrialBalance(period, layout, rows, total_debit, total_credit)
