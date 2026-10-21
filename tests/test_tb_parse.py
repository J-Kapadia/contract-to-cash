"""Trial balance exports (QuickBooks Online and NetSuite layouts, CSV and XLSX) read into one shape."""

from __future__ import annotations

import csv
import zipfile
from pathlib import Path

import pytest

from c2c.errors import PreconditionError, UsageError
from c2c.intake import report, xlsx
from c2c.intake.report import cents, header_index, period_of
from c2c.intake.trial_balance import parse

TB = Path(__file__).parent / "fixtures" / "tb"
QBO = sorted((TB / "qbo").glob("trial_balance_*.csv"))
NETSUITE = TB / "netsuite"


def period_of_name(path: Path) -> str:
    return path.stem.removeprefix("trial_balance_")


def imbalance(tb) -> int:
    return sum(r.debit_cents for r in tb.rows) - sum(r.credit_cents for r in tb.rows)


def copy_csv(tmp_path: Path, source: Path, name: str, edit) -> Path:
    with open(source, newline="", encoding="utf-8") as fh:
        table = edit([row for row in csv.reader(fh)])
    path = tmp_path / name
    with open(path, "w", newline="", encoding="utf-8") as fh:
        csv.writer(fh, lineterminator="\n").writerows(table)
    return path


def test_qbo_fixtures_are_the_planted_months():
    periods = [period_of_name(p) for p in QBO]
    assert len(periods) == 24
    assert periods[0] == "2024-03" and periods[-1] == "2026-03"
    assert "2025-06" not in periods


@pytest.mark.parametrize("path", QBO, ids=lambda p: p.stem)
def test_qbo_file_parses(path):
    tb = parse(path)
    period = period_of_name(path)
    assert tb.period == period
    assert tb.layout == "qbo"
    expected = 13 if period >= "2025-09" else 12
    assert len(tb.rows) == expected
    for r in tb.rows:
        assert isinstance(r.debit_cents, int) and isinstance(r.credit_cents, int)
        assert r.source_file == path.name
    assert tb.total_debit_cents == sum(r.debit_cents for r in tb.rows)
    assert tb.total_credit_cents == sum(r.credit_cents for r in tb.rows)
    assert imbalance(tb) == (12345 if period == "2025-11" else 0)


def test_qbo_account_cells_split_into_id_and_name():
    tb = parse(TB / "qbo" / "trial_balance_2026-03.csv")
    by_id = {r.account_id: r for r in tb.rows}
    assert by_id["1000"].account_name == "Cash"
    assert by_id["6500"].account_name == "Rent"
    assert by_id["6900"].account_name == "Bad debt"
    assert by_id["1000"].debit_cents > 0 and by_id["1000"].credit_cents == 0
    assert by_id["3000"].debit_cents == 0 and by_id["3000"].credit_cents > 0


def test_account_6900_first_appears_in_2025_09():
    first = min(period_of_name(p) for p in QBO if "6900" in {r.account_id for r in parse(p).rows})
    assert first == "2025-09"


@pytest.mark.parametrize("month", ["2026-01", "2026-02", "2026-03"])
def test_xlsx_parses_like_its_csv(month):
    from_csv = parse(TB / "qbo" / f"trial_balance_{month}.csv")
    from_xlsx = parse(TB / "qbo_xlsx" / f"trial_balance_{month}.xlsx")
    assert from_xlsx.period == month and from_xlsx.layout == "qbo"
    assert [(r.account_id, r.account_name, r.debit_cents, r.credit_cents) for r in from_xlsx.rows] == [
        (r.account_id, r.account_name, r.debit_cents, r.credit_cents) for r in from_csv.rows
    ]
    assert (from_xlsx.total_debit_cents, from_xlsx.total_credit_cents) == (
        from_csv.total_debit_cents,
        from_csv.total_credit_cents,
    )
    assert from_xlsx.rows[0].source_file.endswith(".xlsx")


def test_netsuite_files_parse():
    for name, period in (("tb_2026-01.csv", "2026-01"), ("tb_2026-02.csv", "2026-02"), ("tb_latest.csv", "2026-03")):
        tb = parse(NETSUITE / name)
        assert tb.layout == "netsuite"
        assert tb.period == period
        assert len(tb.rows) == 13
        assert imbalance(tb) == 0
        assert tb.total_debit_cents == sum(r.debit_cents for r in tb.rows)
        assert tb.total_credit_cents == sum(r.credit_cents for r in tb.rows)


def test_netsuite_xlsx_matches_its_csv():
    from_csv, from_xlsx = parse(NETSUITE / "tb_latest.csv"), parse(NETSUITE / "tb_2026-03.xlsx")
    assert from_xlsx.layout == "netsuite" and from_xlsx.period == "2026-03"
    assert [r.account_id for r in from_xlsx.rows] == [r.account_id for r in from_csv.rows]
    assert [r.debit_cents for r in from_xlsx.rows] == [r.debit_cents for r in from_csv.rows]
    assert from_xlsx.total_credit_cents == from_csv.total_credit_cents


def test_header_detection_ignores_case_and_spaces():
    assert header_index([["Trial Balance"], [], ["", "Debit", "Credit"]]) == 2
    assert header_index([["x"], ["Account", "DEBIT", " credit "]]) == 1
    assert header_index([["Debit only"], ["", "Debit"]]) is None


def test_header_variants_parse(tmp_path):
    base = TB / "qbo" / "trial_balance_2026-03.csv"

    def shout(table):
        table[4] = ["Account", "DEBIT", " credit "]
        return table

    tb = parse(copy_csv(tmp_path, base, "trial_balance_2026-03.csv", shout))
    assert len(tb.rows) == 13 and tb.layout == "netsuite"
    assert tb.rows[0].account_id == "1000"
    assert parse(base).layout == "qbo"


def test_data_stops_at_total_in_any_case_and_footer_is_ignored(tmp_path):
    base = TB / "qbo" / "trial_balance_2026-03.csv"
    plain = parse(base)

    def lower(table):
        table[next(i for i, row in enumerate(table) if row[:1] == ["TOTAL"])][0] = " total "
        table.append(["9999 Looks like an account", "1.00", ""])
        return table

    tb = parse(copy_csv(tmp_path, base, "trial_balance_2026-03.csv", lower))
    assert len(tb.rows) == len(plain.rows)
    assert tb.total_debit_cents == plain.total_debit_cents


def test_data_stops_at_a_blank_first_cell(tmp_path):
    def cut(table):
        table[8] = ["", "", ""]
        return table

    tb = parse(copy_csv(tmp_path, TB / "qbo" / "trial_balance_2026-03.csv", "trial_balance_2026-03.csv", cut))
    assert len(tb.rows) == 3
    assert tb.total_debit_cents is None and tb.total_credit_cents is None


def test_total_row_that_differs_from_the_sums_still_parses(tmp_path):
    def skew(table):
        i = next(i for i, row in enumerate(table) if row[:1] == ["TOTAL"])
        table[i] = ["TOTAL", "1.00", "2.50"]
        return table

    tb = parse(copy_csv(tmp_path, TB / "qbo" / "trial_balance_2026-03.csv", "trial_balance_2026-03.csv", skew))
    assert (tb.total_debit_cents, tb.total_credit_cents) == (100, 250)
    assert imbalance(tb) == 0


def test_no_total_row_leaves_both_totals_none(tmp_path):
    def drop(table):
        i = next(i for i, row in enumerate(table) if row[:1] == ["TOTAL"])
        return table[:i]

    tb = parse(copy_csv(tmp_path, TB / "qbo" / "trial_balance_2026-03.csv", "trial_balance_2026-03.csv", drop))
    assert len(tb.rows) == 13
    assert tb.total_debit_cents is None and tb.total_credit_cents is None


def test_account_cell_without_a_number_keeps_the_text(tmp_path):
    def rename(table):
        table[5][0] = "Petty cash"
        table[6][0] = "3D printing"
        table[7][0] = "1500"
        return table

    tb = parse(copy_csv(tmp_path, TB / "qbo" / "trial_balance_2026-03.csv", "trial_balance_2026-03.csv", rename))
    assert [(r.account_id, r.account_name) for r in tb.rows[:3]] == [
        ("Petty cash", "Petty cash"),
        ("3D printing", "3D printing"),
        ("1500", "1500"),
    ]


def test_bad_amount_names_the_file(tmp_path):
    def spoil(table):
        table[5][1] = "abc"
        return table

    path = copy_csv(tmp_path, TB / "qbo" / "trial_balance_2026-03.csv", "trial_balance_2026-03.csv", spoil)
    with pytest.raises(UsageError, match="trial_balance_2026-03.csv"):
        parse(path)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("1,234.56", 123456),
        ("-1,234.56", -123456),
        ("(1,234.56)", -123456),
        ("$1,234.56", 123456),
        ("-$1,234.56", -123456),
        ("($1,234.56)", -123456),
        ("1234.5", 123450),
        ("1234", 123400),
        ("0.05", 5),
        (".5", 50),
        (" 12.00 ", 1200),
        ("", 0),
        ("  ", 0),
        ("-", 0),
    ],
)
def test_cents(text, expected):
    assert cents(text) == expected
    assert isinstance(cents(text), int)


@pytest.mark.parametrize("text", ["abc", "1.234", "1,23.00", "12,34", "1.2.3", "$", ".", "--5", "1 234.00"])
def test_cents_rejects_what_is_not_money(text):
    with pytest.raises(UsageError):
        cents(text)


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("trial_balance_2026-03.csv", "2026-03"),
        ("tb_2026_03.xlsx", "2026-03"),
        ("tb202603.csv", "2026-03"),
        ("trial_balance_202603.csv", "2026-03"),
        ("Trial Balance Mar 2026.csv", "2026-03"),
        ("trial balance March 2026.xlsx", "2026-03"),
        ("TB_December_2025.csv", "2025-12"),
    ],
)
def test_period_from_the_file_name(name, expected):
    assert period_of(Path(name), [["As of June 30, 2020"]]) == expected


@pytest.mark.parametrize(
    ("title", "expected"),
    [
        ("As of March 31, 2026", "2026-03"),
        ("End of Mar 2026", "2026-03"),
        ("March 2026", "2026-03"),
        ("January - March 2026", "2026-03"),
        ("January 2026 - February 2026", "2026-02"),
        ("As of Sep 30, 2025", "2025-09"),
    ],
)
def test_period_from_a_title_row(title, expected):
    assert period_of(Path("tb_latest.csv"), [["Synthetic Co (synthetic)"], ["Trial Balance"], [title]]) == expected


def test_period_missing_names_the_file():
    with pytest.raises(UsageError, match="tb_latest.csv"):
        period_of(Path("tb_latest.csv"), [["Trial Balance"]])
    with pytest.raises(UsageError, match="20260331"):
        period_of(Path("20260331.csv"), [])


def test_no_debit_credit_header_raises_usage_error(tmp_path):
    path = tmp_path / "trial_balance_2026-03.csv"
    path.write_text("Trial Balance\n\nAccount,Amount\n1000 Cash,1.00\n", encoding="utf-8")
    with pytest.raises(UsageError, match="trial_balance_2026-03.csv") as caught:
        parse(path)
    assert "Debit" in str(caught.value) and "Credit" in str(caught.value)


def test_unknown_suffix_is_a_usage_error(tmp_path):
    path = tmp_path / "trial_balance_2026-03.pdf"
    path.write_bytes(b"%PDF")
    with pytest.raises(UsageError, match="trial_balance_2026-03.pdf"):
        report.rows_of(path)


def test_csv_with_a_byte_order_mark_parses(tmp_path):
    source = (TB / "qbo" / "trial_balance_2026-03.csv").read_text(encoding="utf-8")
    path = tmp_path / "trial_balance_2026-03.csv"
    path.write_text(source, encoding="utf-8-sig")
    assert parse(path).layout == "qbo"


def test_xlsx_that_is_not_a_zip_raises_precondition_error(tmp_path):
    path = tmp_path / "trial_balance_2026-03.xlsx"
    path.write_text("Trial Balance,,\n", encoding="utf-8")
    with pytest.raises(PreconditionError, match="trial_balance_2026-03.xlsx"):
        parse(path)


def test_xlsx_without_a_worksheet_raises_precondition_error(tmp_path):
    path = tmp_path / "empty.xlsx"
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("docProps/app.xml", "<x/>")
    with pytest.raises(PreconditionError, match="empty.xlsx"):
        xlsx.rows(path)


MAIN = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"


def write_workbook(path: Path, sheet: str, styles: str = "", strings: str | None = None) -> None:
    rels = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
    with zipfile.ZipFile(path, "w") as z:
        z.writestr(
            "xl/workbook.xml",
            f'<workbook xmlns="{MAIN}" xmlns:r="{rels}"><sheets>'
            '<sheet name="One" sheetId="1" r:id="rId7"/><sheet name="Two" sheetId="2" r:id="rId8"/></sheets></workbook>',
        )
        z.writestr(
            "xl/_rels/workbook.xml.rels",
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId8" Type="x" Target="worksheets/sheet2.xml"/>'
            '<Relationship Id="rId7" Type="x" Target="worksheets/sheet9.xml"/></Relationships>',
        )
        z.writestr("xl/worksheets/sheet9.xml", f'<worksheet xmlns="{MAIN}"><sheetData>{sheet}</sheetData></worksheet>')
        z.writestr("xl/worksheets/sheet2.xml", f'<worksheet xmlns="{MAIN}"><sheetData/></worksheet>')
        if styles:
            z.writestr("xl/styles.xml", f'<styleSheet xmlns="{MAIN}">{styles}</styleSheet>')
        if strings is not None:
            z.writestr("xl/sharedStrings.xml", f'<sst xmlns="{MAIN}">{strings}</sst>')


def test_xlsx_cell_types(tmp_path):
    styles = (
        '<numFmts count="2"><numFmt numFmtId="164" formatCode="yyyy\\-mm\\-dd"/>'
        '<numFmt numFmtId="165" formatCode="0.00&quot;m d y&quot;"/></numFmts>'
        '<cellXfs count="5"><xf numFmtId="0"/><xf numFmtId="14"/><xf numFmtId="164"/><xf numFmtId="165"/>'
        '<xf numFmtId="22"/></cellXfs>'
    )
    sheet = (
        '<row r="1"><c r="A1" t="s"><v>0</v></c><c r="C1" t="inlineStr"><is><t>inline</t></is></c></row>'
        '<row r="2"><c r="A2" t="b"><v>1</v></c><c r="B2" t="b"><v>0</v></c><c r="C2"><v>12.50</v></c></row>'
        '<row r="4"><c r="A4" s="1"><v>46112</v></c><c r="B4" s="2"><v>45000</v></c>'
        '<c r="C4" s="3"><v>45000</v></c><c r="D4" s="4"><v>46112.75</v></c>'
        '<c r="E4" t="str"><f>A1</f><v>cached</v></c></row>'
    )
    strings = '<si><t>a</t></si><si><r><t>rich </t></r><r><t>text</t></r></si>'
    path = tmp_path / "cells.xlsx"
    write_workbook(path, sheet, styles, strings)
    assert xlsx.rows(path) == [
        ["a", "", "inline", "", ""],
        ["TRUE", "FALSE", "12.50", "", ""],
        ["", "", "", "", ""],
        ["2026-03-31", "2023-03-15", "45000", "2026-03-31", "cached"],
    ]


def test_xlsx_reads_the_first_sheet_and_shared_strings_with_runs(tmp_path):
    path = tmp_path / "runs.xlsx"
    write_workbook(path, '<row r="1"><c r="A1" t="s"><v>1</v></c></row>', strings="<si><t>a</t></si><si><r><t>rich </t></r><r><t>text</t></r></si>")
    assert xlsx.rows(path) == [["rich text"]]
