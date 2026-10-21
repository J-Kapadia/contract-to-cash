"""The first worksheet of an .xlsx as text cells, read with the standard library.

No formulas are evaluated: a formula cell is its cached value. A cell is text as the sheet shows it,
except a date, which is `YYYY-MM-DD`: the style of the cell says whether its number is a date.

Choices made where the brief is silent: a row the sheet leaves out is a row of empty cells, so a
blank line keeps its place; a style that cannot be found is not a date.
"""

from __future__ import annotations

import re
import zipfile
from datetime import date, timedelta
from pathlib import Path
from xml.etree import ElementTree

from ..errors import PreconditionError

_MAIN = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
_REL = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
_PKG = "{http://schemas.openxmlformats.org/package/2006/relationships}"

_EPOCH = date(1899, 12, 30)
_BUILTIN_DATE_FORMATS = range(14, 23)


def _xml(archive: zipfile.ZipFile, name: str) -> ElementTree.Element:
    return ElementTree.fromstring(archive.read(name))


def _text(node: ElementTree.Element) -> str:
    """All the text under a shared string or inline string, rich-text runs joined."""
    return "".join(t.text or "" for t in node.iter(f"{_MAIN}t"))


def _shared_strings(archive: zipfile.ZipFile) -> list[str]:
    if "xl/sharedStrings.xml" not in archive.namelist():
        return []
    return [_text(si) for si in _xml(archive, "xl/sharedStrings.xml").iter(f"{_MAIN}si")]


def _is_date_format(code: str) -> bool:
    code = re.sub(r'"[^"]*"|\[[^\]]*\]|\\.', "", code).lower()
    return all(letter in code for letter in "ymd")


def _date_styles(archive: zipfile.ZipFile) -> set[int]:
    """The indexes of the cell styles whose number format is a date."""
    if "xl/styles.xml" not in archive.namelist():
        return set()
    styles = _xml(archive, "xl/styles.xml")
    custom = {
        int(f.get("numFmtId")): _is_date_format(f.get("formatCode", ""))
        for f in styles.iter(f"{_MAIN}numFmt")
    }
    xfs = styles.find(f"{_MAIN}cellXfs")
    if xfs is None:
        return set()
    found = set()
    for index, xf in enumerate(xfs.findall(f"{_MAIN}xf")):
        fmt = int(xf.get("numFmtId", "0"))
        if fmt in _BUILTIN_DATE_FORMATS or custom.get(fmt, False):
            found.add(index)
    return found


def _first_sheet(archive: zipfile.ZipFile, path: Path) -> str:
    names = archive.namelist()
    if "xl/workbook.xml" not in names:
        raise PreconditionError(f"{path}: not a spreadsheet workbook; export the report as .xlsx again")
    sheet = _xml(archive, "xl/workbook.xml").find(f"{_MAIN}sheets/{_MAIN}sheet")
    if sheet is None or "xl/_rels/workbook.xml.rels" not in names:
        raise PreconditionError(f"{path}: the workbook has no worksheet")
    wanted = sheet.get(f"{_REL}id")
    for rel in _xml(archive, "xl/_rels/workbook.xml.rels").iter(f"{_PKG}Relationship"):
        if rel.get("Id") == wanted:
            target = rel.get("Target", "")
            part = target.lstrip("/") if target.startswith("/") else f"xl/{target}"
            if part in names:
                return part
    raise PreconditionError(f"{path}: the workbook has no worksheet")


def _column(ref: str) -> int:
    letters = re.match(r"[A-Za-z]+", ref).group(0).upper()
    index = 0
    for letter in letters:
        index = index * 26 + ord(letter) - ord("A") + 1
    return index - 1


def _cell(node: ElementTree.Element, strings: list[str], dates: set[int]) -> str:
    kind = node.get("t", "n")
    if kind == "inlineStr":
        inline = node.find(f"{_MAIN}is")
        return "" if inline is None else _text(inline)
    v = node.find(f"{_MAIN}v")
    value = "" if v is None or v.text is None else v.text
    if kind == "s":
        return strings[int(value)] if value else ""
    if kind == "b":
        return "TRUE" if value.strip() == "1" else "FALSE"
    if kind in ("n", "") and value and int(node.get("s", "0")) in dates:
        try:
            return (_EPOCH + timedelta(days=int(float(value)))).isoformat()
        except (ValueError, OverflowError):
            return value
    return value


def rows(path: str | Path) -> list[list[str]]:
    """The cells of the first worksheet of the .xlsx at `path`, every row as wide as the widest."""
    path = Path(path)
    try:
        archive = zipfile.ZipFile(path)
    except (zipfile.BadZipFile, OSError) as exc:
        raise PreconditionError(f"{path}: not an .xlsx file ({exc}); export the report as .xlsx again") from exc
    with archive:
        try:
            part = _first_sheet(archive, path)
            strings, dates = _shared_strings(archive), _date_styles(archive)
            sheet = _xml(archive, part)
        except (KeyError, ElementTree.ParseError, ValueError) as exc:
            raise PreconditionError(f"{path}: cannot read the worksheet ({exc})") from exc
    table: dict[int, dict[int, str]] = {}
    for position, row in enumerate(sheet.iter(f"{_MAIN}row")):
        number = int(row.get("r")) - 1 if row.get("r") else position
        cells = table.setdefault(number, {})
        for column, node in enumerate(row.findall(f"{_MAIN}c")):
            ref = node.get("r")
            cells[_column(ref) if ref else column] = _cell(node, strings, dates)
    if not table:
        return []
    width = max((max(cells) + 1 for cells in table.values() if cells), default=0)
    return [[table.get(n, {}).get(c, "") for c in range(width)] for n in range(max(table) + 1)]
