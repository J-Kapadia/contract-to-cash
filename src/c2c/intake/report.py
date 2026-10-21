"""What the report exports of other systems share: the rows of a file, report money, the period a
report covers and the row that heads its columns.

Choices made where the brief is silent: `cents` takes a minus sign, parentheses or both, before or
after a `$`; a month in a file name is found only when it is not part of a longer run of digits or
letters.
"""

from __future__ import annotations

import csv
import re
from pathlib import Path

from ..errors import UsageError
from . import xlsx


def _padded(table: list[list[str]]) -> list[list[str]]:
    width = max((len(row) for row in table), default=0)
    return [row + [""] * (width - len(row)) for row in table]


def rows_of(path: str | Path) -> list[list[str]]:
    """The cells of a .csv or .xlsx report, every row as wide as the widest."""
    path = Path(path)
    suffix = path.suffix.lower()
    if suffix == ".csv":
        with open(path, newline="", encoding="utf-8-sig") as fh:
            return _padded([list(row) for row in csv.reader(fh)])
    if suffix == ".xlsx":
        return xlsx.rows(path)
    raise UsageError(f"{path}: a report is a .csv or an .xlsx file")


_MONEY = re.compile(r"^(?P<sign>-)?\$?(?P<whole>\d{1,3}(?:,\d{3})+|\d+)?(?:\.(?P<frac>\d*))?$")


def cents(text: str) -> int:
    """Report money as integer cents: `1,234.56`, `-1,234.56`, `(1,234.56)`, `$1,234.56`, `1234.5`;
    blank and `-` are 0."""
    s = (text or "").strip()
    if s in ("", "-"):
        return 0
    negative = s.startswith("(") and s.endswith(")")
    if negative:
        s = s[1:-1].strip()
    m = _MONEY.match(s)
    if m is None or (m["whole"] is None and not m["frac"]):
        raise UsageError(f"not an amount of money: {text!r}")
    frac = m["frac"] or ""
    if len(frac) > 2:
        raise UsageError(f"more than two decimals in an amount of money: {text!r}")
    value = int((m["whole"] or "0").replace(",", "")) * 100 + int(frac.ljust(2, "0"))
    return -value if negative or m["sign"] else value


_MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}  # fmt: skip
_NAME = (
    r"(?P<month>january|february|march|april|may|june|july|august|september|october|november|december"
    r"|jan|feb|mar|apr|jun|jul|aug|sep|sept|oct|nov|dec)"
)
_NAME_IN_FILE = re.compile(rf"(?<![A-Za-z]){_NAME}[\s_.-]*(?P<year>\d{{4}})(?!\d)", re.IGNORECASE)
_NAME_IN_TITLE = re.compile(
    rf"(?<![A-Za-z]){_NAME}\.?\s+(?:\d{{1,2}}(?:st|nd|rd|th)?,?\s+)?(?P<year>\d{{4}})(?!\d)", re.IGNORECASE
)
_ISO_IN_FILE = re.compile(r"(?<!\d)(?P<year>\d{4})[-_](?P<month>0[1-9]|1[0-2])(?!\d)")
_COMPACT_IN_FILE = re.compile(r"(?<=[A-Za-z_-])(?P<year>\d{4})(?P<month>0[1-9]|1[0-2])(?!\d)")


def _month(name: str) -> int:
    return _MONTHS[name[:3].lower()]


def _of_match(m: re.Match) -> str:
    month = m["month"]
    return f"{m['year']}-{int(month) if month.isdigit() else _month(month):02d}"


def period_of(path: str | Path, title_rows: list[list[str]]) -> str:
    """The month a report covers as `YYYY-MM`: from the file name when it names one, else from the
    title rows (a range takes its last month)."""
    path = Path(path)
    for pattern in (_ISO_IN_FILE, _COMPACT_IN_FILE, _NAME_IN_FILE):
        m = pattern.search(path.name)
        if m:
            return _of_match(m)
    for row in title_rows:
        for cell in row:
            found = list(_NAME_IN_TITLE.finditer(cell))
            if found:
                return _of_match(found[-1])
    raise UsageError(
        f"{path}: cannot tell which month the report covers; put the month in the file name "
        "(for example trial_balance_2026-03.csv)"
    )


def header_index(rows: list[list[str]]) -> int | None:
    """The index of the first row with a `Debit` cell and a `Credit` cell, or None."""
    for index, row in enumerate(rows):
        cells = {cell.strip().lower() for cell in row}
        if "debit" in cells and "credit" in cells:
            return index
    return None
