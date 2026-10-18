"""The single-definition lint over the dbt models.

Ratable revenue is defined once, in the `ratable_cents()` macro, and an expected cash date once,
in `expected_pay_date()`. No model may do its own proration arithmetic over `amount_cents` or a
`service_` column (`date_diff`, `datediff`, `//`, or a multiply followed by a divide), and none
may add days or an interval to `invoice_date`. Macros are not scanned: they are where the
definitions live. A line that really needs such arithmetic carries, on itself or on the line
above, the comment

    -- single-definition: ok <reason>

and a reason is required.

The scan is line by line, after comments and string literals are blanked, so an expression
split across lines is not seen: keep the arithmetic on one line or do not write it.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

MODELS = Path(__file__).resolve().parents[1] / "dbt" / "models"

ALLOW = re.compile(r"--\s*single-definition:\s*ok\s+\S")

COLUMNS = re.compile(r"\b(amount_cents|service_\w+)\b", re.I)
PRORATION = re.compile(r"\bdate_?diff\s*\(|//|\*[^/]*/", re.I)
INVOICE_DATE = re.compile(r"\binvoice_date\b", re.I)
PAY_DATE = re.compile(r"\binvoice_date\b[^,;]*?\+|\binterval\b|\bdate_add\s*\(", re.I)

PRORATION_MESSAGE = ("proration arithmetic over {col}: recognise revenue by calling ratable_cents() "
                     "(or split a weighted set with split_exact()) instead")
PAY_DATE_MESSAGE = ("date arithmetic on invoice_date: compute an expected cash date by calling "
                    "expected_pay_date() instead")


def blank(sql: str) -> str:
    """The SQL with comments and string literals replaced by spaces, line structure kept."""
    def spaces(match: re.Match) -> str:
        return re.sub(r"[^\n]", " ", match.group(0))
    pattern = r"'(?:[^']|'')*'|/\*.*?\*/|\{#.*?#\}|--[^\n]*"
    return re.sub(pattern, spaces, sql, flags=re.S)


def violations(sql: str) -> list[tuple[int, str]]:
    """The (line number, message) of each line of `sql` that breaks the rules and is not allowed."""
    raw_lines = sql.splitlines()
    found = []
    for number, line in enumerate(blank(sql).splitlines(), start=1):
        allowed = ALLOW.search(raw_lines[number - 1]) or (
            number > 1 and ALLOW.search(raw_lines[number - 2]))
        if allowed:
            continue
        column = COLUMNS.search(line)
        if column and PRORATION.search(line):
            found.append((number, PRORATION_MESSAGE.format(col=column.group(1))))
        if INVOICE_DATE.search(line) and PAY_DATE.search(line):
            found.append((number, PAY_DATE_MESSAGE))
    return found


def model_files() -> list[Path]:
    return sorted(MODELS.rglob("*.sql"))


@pytest.mark.parametrize("path", model_files(), ids=lambda p: str(p.relative_to(MODELS)))
def test_model_has_no_second_definition(path):
    found = violations(path.read_text())
    assert not found, "\n".join(f"{path.relative_to(MODELS.parent)}:{n}: {m}" for n, m in found)


def test_there_are_models_to_scan():
    assert model_files()


# -- the lint itself ---------------------------------------------------------------------------

@pytest.mark.parametrize("line", [
    "select date_diff('day', service_start, service_end) as days from t",
    "select datediff('day', service_start, service_end) from t",
    "select amount_cents * days_in_month // days_in_period from t",
    "select amount_cents * elapsed / total as share from t",
    "select (amount_cents * weight_bps) // 10000 from t",
    "select service_end - service_start // 2 from t",
])
def test_proration_arithmetic_is_flagged(line):
    assert violations(line)


@pytest.mark.parametrize("line", [
    "select invoice_date + 30 as expected_on from t",
    "select invoice_date + interval 45 day from t",
    "select invoice_date + dtp_days from t",
    "select date_add(invoice_date, interval 30 day) from t",
])
def test_expected_pay_date_arithmetic_is_flagged(line):
    assert violations(line)


@pytest.mark.parametrize("line", [
    "select sum(amount_cents) as billed from t",
    "select count(*) / 2 from t",
    "select * from t where invoice_date >= date '2024-01-01'",
    "select amount_cents from t  -- amount_cents // 2 in a comment",
    "select 'amount_cents // 2' as label from t",
    "select {{ ratable_cents('amount_cents', 'service_start', 'service_end', 'month') }} as cents from t",
    "select paid_on - invoice_date as days_to_pay from t",
    "select month + interval 1 month from t",
])
def test_ordinary_sql_is_not_flagged(line):
    assert violations(line) == []


def test_an_allowance_with_a_reason_covers_its_line_and_the_next():
    same_line = "select amount_cents // 2 from t -- single-definition: ok a fixed halving, not proration"
    line_above = ("-- single-definition: ok a fixed halving, not proration\n"
                  "select amount_cents // 2 from t")
    assert violations(same_line) == []
    assert violations(line_above) == []


def test_an_allowance_needs_a_reason_and_covers_only_one_line():
    assert violations("select amount_cents // 2 from t -- single-definition: ok")
    assert violations("-- single-definition: ok halving\nselect 1\nselect amount_cents // 2 from t")


def test_violations_report_the_line_number():
    assert [n for n, _ in violations("select 1\nselect amount_cents // 2 from t\n")] == [2]
