"""Expected cash dates and amounts over the `mini` books.

Every row of the mart equals the expected date and amount worked out here from the books and the
rules written out in `reference_date`; the macro is also run on its own over hand-made dates for the
cap, the weekends and the missing inputs. A copy of the books with three unpaid invoices added
covers the at-risk threshold.
"""

from __future__ import annotations

import csv
import functools
import re
import shutil
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path

import jinja2
import pytest

from c2c import db, dbt_runner, load, policy
from c2c import truth_reference as tr
from conftest import DEFAULT_CONFIG, MINI_BOOKS, MINI_WINDOW, ROOT, shared_dir

REFERENCE = ROOT / "data" / "reference"
MACRO = ROOT / "dbt" / "macros" / "expected_pay_date.sql"
POLICY = policy.load(DEFAULT_CONFIG)
POOLED = "*"
CASES = ("base", "downside", "upside")
MONTH = MINI_WINDOW[1]
AS_OF = date(2026, 3, 31)

# (invoice id, days past due at 2026-03-31): the threshold is 120.
ADDED = [("inv_900001", 120), ("inv_900002", 121), ("inv_900003", 130)]


def build(warehouse: Path, books: Path = MINI_BOOKS) -> None:
    con = db.connect(warehouse)
    try:
        db.init_schemas(con)
        load.load_months(con, books, *MINI_WINDOW, "test")
        load.load_reference(con, REFERENCE)
        load.load_config(con, DEFAULT_CONFIG)
    finally:
        con.close()
    dbt_runner.build(warehouse, select="+fct_expected_cash")


def query(warehouse: Path, sql: str, params=()):
    con = db.connect(warehouse, read_only=True)
    try:
        return con.execute(sql, params).fetchall()
    finally:
        con.close()


def csv_rows(path: Path) -> list[dict]:
    with path.open(encoding="utf-8", newline="") as fh:
        return list(csv.DictReader(fh))


def mart(warehouse: Path, month: str | None = None) -> dict[tuple[str, str, str], tuple[str, date, int, bool]]:
    where, params = ("WHERE month = ?", [month]) if month else ("", [])
    return {(m, invoice, case): (customer, on, cents, risk) for m, invoice, customer, case, on, cents, risk in query(
        warehouse, f'SELECT month, invoice_id, customer_id, "case", expected_date, cents, at_risk'
                   f" FROM mart.fct_expected_cash {where}", params)}


@pytest.fixture(scope="module")
def mini_cash(tmp_path_factory) -> Path:
    """The `mini` books loaded and expected cash built (with its dbt tests). Read-only."""
    return shared_dir(tmp_path_factory, "expected-cash-mini", lambda d: build(d / "w.duckdb")) / "w.duckdb"


@pytest.fixture(scope="module")
def at_risk_cash(tmp_path_factory) -> Path:
    """The `mini` books plus three unpaid invoices dated 30 days before their due dates."""
    def make(directory: Path) -> None:
        books = directory / "books"
        shutil.copytree(MINI_BOOKS, books)
        template = csv_rows(books / "invoices.csv")[0]
        with (books / "invoices.csv").open("a", encoding="utf-8", newline="") as fh:
            writer = csv.DictWriter(fh, fieldnames=list(template), lineterminator="\n")
            for invoice, late in ADDED:
                due = AS_OF - timedelta(days=late)
                issued = due - timedelta(days=30)
                writer.writerow({**template, "invoice_id": invoice, "line_no": 1, "invoice_date": issued.isoformat(),
                                 "gl_period": issued.strftime("%Y-%m"), "due_date": due.isoformat(),
                                 "service_start": issued.isoformat(), "service_end": (issued + timedelta(days=364)).isoformat(),
                                 "line_type": "subscription", "amount_cents": 1_000_000, "known_on": issued.isoformat()})
        build(directory / "w.duckdb", books)
    return shared_dir(tmp_path_factory, "expected-cash-at-risk", make) / "w.duckdb"


# ---------------------------------------------------------------- the rules, written out again

@functools.cache
def calendar() -> tuple[date, ...]:
    return tuple(date.fromisoformat(r["date"]) for r in csv_rows(REFERENCE / "business_days.csv")
                 if r["is_business_day"] == "1")


def next_business_day(day: date) -> date:
    return min(d for d in calendar() if d >= day)


def last_business_day(day: date) -> date:
    return max(d for d in calendar() if d <= day)


def reference_date(invoice_date: date, due_date: date, as_of: date, dtp: int | None, lateness: int | None) -> date:
    nominal = invoice_date + timedelta(days=(due_date - invoice_date).days if dtp is None else dtp)
    allowed = as_of + timedelta(days=91)
    wanted = nominal if nominal > as_of else min(as_of + timedelta(days=1 + (lateness or 0)), allowed)
    moved = next_business_day(wanted)
    return moved if nominal > as_of or moved <= allowed else last_business_day(allowed)


def ym(month: str) -> int:
    return int(month[:4]) * 12 + int(month[5:]) - 1


class Books:
    """Open invoices, days to pay and lateness worked out from a books directory."""

    def __init__(self, books: Path):
        owners = {r["contract_id"]: r["crm_account_id"] for r in csv_rows(books / "contracts.csv")}
        self.cents: dict[str, int] = defaultdict(int)
        self.dates: dict[str, tuple[date, date]] = {}
        self.customer: dict[str, str] = {}
        for r in csv_rows(books / "invoices.csv"):
            self.cents[r["invoice_id"]] += int(r["amount_cents"])
            self.dates[r["invoice_id"]] = (date.fromisoformat(r["invoice_date"]), date.fromisoformat(r["due_date"]))
            self.customer[r["invoice_id"]] = owners[r["contract_id"]]
        self.applications: dict[str, list[tuple[date, int]]] = defaultdict(list)
        for r in csv_rows(books / "payments.csv"):
            self.applications[r["invoice_id"]].append((date.fromisoformat(r["applied_on"]), int(r["applied_cents"])))
        self.paid: dict[str, date] = {}
        for invoice, parts in self.applications.items():
            total = 0
            for on, cents in sorted(parts):
                total += cents
                if total >= self.cents[invoice]:
                    self.paid[invoice] = on
                    break

    def window(self, month: str) -> list[tuple[str, int, int]]:
        """`(customer, days to pay, days late)` of the invoices paid in the trailing window of `month`."""
        return [(self.customer[i], (on - self.dates[i][0]).days, (on - self.dates[i][1]).days)
                for i, on in self.paid.items()
                if ym(month) - POLICY.dtp_window_months < ym(tr.month_of(on)) <= ym(month)]

    def days(self, month: str, customer: str) -> tuple[int | None, int | None, int | None]:
        """`(median days, p80 days, median lateness)` of the customer, or of everyone below the minimum."""
        rows = self.window(month)
        own = [r for r in rows if r[0] == customer]
        use = own if len(own) >= POLICY.dtp_min_paid_invoices else rows
        if not use:
            return None, None, None
        return (tr.lower_median([r[1] for r in use]), tr.percentile_nearest_rank([r[1] for r in use], 80),
                max(tr.lower_median([r[2] for r in use]), 0))

    def open_at(self, as_of: date) -> dict[str, int]:
        out = {}
        for invoice, (issued, _) in self.dates.items():
            applied = sum(c for on, c in self.applications.get(invoice, []) if on <= as_of)
            if issued <= as_of and self.cents[invoice] > applied:
                out[invoice] = self.cents[invoice] - applied
        return out

    def expected(self, month: str) -> dict[tuple[str, str, str], tuple[str, date, int, bool]]:
        as_of = tr.month_end(month)
        out = {}
        for invoice, cents in self.open_at(as_of).items():
            issued, due = self.dates[invoice]
            customer = self.customer[invoice]
            median, p80, lateness = self.days(month, customer)
            terms = (due - issued).days
            risk = (as_of - due).days > POLICY.at_risk_days_past_due
            for case, dtp in (("base", median), ("downside", p80),
                              ("upside", terms if median is None else min(median, terms))):
                out[(month, invoice, case)] = (
                    customer, reference_date(issued, due, as_of, dtp, lateness), 0 if risk and case != "downside" else cents, risk)
        return out


MONTHS = tr.months_between(*MINI_WINDOW)


# ---------------------------------------------------------------- the mart over the mini books

def test_the_mart_carries_the_standard_columns(mini_cash):
    columns = [row[0] for row in query(
        mini_cash, "SELECT column_name FROM information_schema.columns"
                   " WHERE table_schema = 'mart' AND table_name = 'fct_expected_cash' ORDER BY ordinal_position")]
    assert columns == ["month", "invoice_id", "customer_id", "case", "expected_date", "cents", "at_risk"]


def test_every_open_invoice_has_a_row_per_case_equal_to_the_rules_applied_to_the_books(mini_cash):
    books = Books(MINI_BOOKS)
    expected = {}
    for month in MONTHS:
        expected.update(books.expected(month))
    assert len({invoice for _, invoice, _ in expected}) > 10
    assert {case for _, _, case in expected} == set(CASES)
    assert mart(mini_cash) == expected


def test_march_dates_are_business_days_after_month_end(mini_cash):
    rows = mart(mini_cash, MONTH)
    assert rows
    for (_, invoice, case), (_, on, _, _) in rows.items():
        assert on in calendar() and on > AS_OF, (invoice, case, on)


def test_a_customer_with_too_few_paid_invoices_is_dated_by_the_pooled_days(mini_cash):
    books = Books(MINI_BOOKS)
    rows = mart(mini_cash)
    short = [(month, invoice) for (month, invoice, case), (customer, *_) in rows.items()
             if case == "base" and books.window(month)
             and len([r for r in books.window(month) if r[0] == customer]) < POLICY.dtp_min_paid_invoices]
    assert short, "mini has no open invoice of a customer below the paid-invoice minimum"
    for month, invoice in short:
        customer, on, _, _ = rows[(month, invoice, "base")]
        issued, due = books.dates[invoice]
        pooled = books.window(month)
        lateness = max(tr.lower_median([r[2] for r in pooled]), 0)
        assert on == reference_date(issued, due, tr.month_end(month), tr.lower_median([r[1] for r in pooled]), lateness)


def test_before_any_invoice_is_paid_the_terms_date_the_invoice(mini_cash):
    books = Books(MINI_BOOKS)
    early = [(month, invoice) for (month, invoice, case) in mart(mini_cash) if case == "base" and not books.window(month)]
    assert early, "mini has no month before the first paid invoice with an open invoice"
    for month, invoice in early:
        issued, due = books.dates[invoice]
        assert mart(mini_cash, month)[(month, invoice, "base")][1] == reference_date(issued, due, tr.month_end(month), None, None)


def test_a_past_expected_date_is_shifted_to_the_day_after_month_end_plus_lateness(mini_cash):
    books = Books(MINI_BOOKS)
    rows = mart(mini_cash, MONTH)
    shifted = 0
    for (_, invoice, case), (customer, on, _, _) in rows.items():
        issued, due = books.dates[invoice]
        median, p80, lateness = books.days(MONTH, customer)
        dtp = {"base": median, "downside": p80, "upside": min(median, (due - issued).days)}[case]
        if issued + timedelta(days=dtp) <= AS_OF:
            shifted += 1
            assert on == next_business_day(AS_OF + timedelta(days=1 + lateness)), (invoice, case)
    assert shifted, "mini has no open invoice whose expected date is already past"


# ---------------------------------------------------------------- the at-risk threshold

def test_an_invoice_130_days_past_due_is_at_risk_and_brings_no_base_or_upside_cash(at_risk_cash):
    rows = mart(at_risk_cash, MONTH)
    assert rows[(MONTH, "inv_900003", "base")][2:] == (0, True)
    assert rows[(MONTH, "inv_900003", "upside")][2:] == (0, True)
    assert rows[(MONTH, "inv_900003", "downside")][2:] == (1_000_000, True)


def test_the_threshold_is_more_than_120_days_past_due(at_risk_cash):
    rows = mart(at_risk_cash, MONTH)
    assert rows[(MONTH, "inv_900001", "base")][2:] == (1_000_000, False)
    assert rows[(MONTH, "inv_900002", "base")][2:] == (0, True)
    assert {rows[(MONTH, i, "base")][3] for i, _ in ADDED} == {True, False}
    assert not any(risk for (_, invoice, _), (*_, risk) in rows.items() if not invoice.startswith("inv_9"))


def test_the_added_books_equal_the_rules_too(at_risk_cash):
    shared = at_risk_cash.parent / "books"
    assert mart(at_risk_cash, MONTH) == Books(shared).expected(MONTH)


# ---------------------------------------------------------------- the macro on its own

@pytest.fixture(scope="module")
def macro_db(tmp_path_factory):
    con = db.connect(tmp_path_factory.mktemp("calendar") / "calendar.duckdb")
    con.execute("CREATE TABLE cal (\"date\" VARCHAR, is_business_day VARCHAR)")
    con.executemany("INSERT INTO cal VALUES (?, ?)", [(r["date"], r["is_business_day"]) for r in csv_rows(REFERENCE / "business_days.csv")])
    yield con
    con.close()


def macro_date(con, invoice_date, due_date, as_of, dtp, lateness) -> date:
    source = MACRO.read_text()
    call = "{{ expected_pay_date('d', 'due', 'a', 'dtp', 'late', 'cal') }}"
    expression = jinja2.Template(source + call).render()
    row = con.execute(f"SELECT {expression} FROM (SELECT ?::date AS d, ?::date AS due, ?::date AS a, ?::int AS dtp, ?::int AS late)",
                      [invoice_date, due_date, as_of, dtp, lateness]).fetchone()
    return row[0]


D = date.fromisoformat


@pytest.mark.parametrize("invoice, due, as_of, dtp, lateness, expected", [
    # a future date on a Saturday moves to Monday
    ("2026-03-10", "2026-04-09", "2026-03-31", 25, 0, "2026-04-06"),
    # a future business day stays
    ("2026-03-10", "2026-04-09", "2026-03-31", 30, 0, "2026-04-09"),
    # a past date becomes the day after as-of plus lateness (2026-04-10 is a Friday)
    ("2026-01-10", "2026-02-09", "2026-03-31", 30, 9, "2026-04-10"),
    # as-of itself is past; no lateness means the next day
    ("2026-03-01", "2026-03-31", "2026-03-31", 30, None, "2026-04-01"),
    # lateness beyond the horizon stops at the 91st day, 2026-06-30
    ("2026-01-10", "2026-02-09", "2026-03-31", 30, 500, "2026-06-30"),
    # the 91st day is a Saturday (as-of 2026-03-28): the last business day on or before it
    ("2026-01-10", "2026-02-09", "2026-03-28", 30, 500, "2026-06-26"),
    # no days to pay: the terms stand in
    ("2026-03-10", "2026-04-17", "2026-03-31", None, 0, "2026-04-17"),
])
def test_the_macro_dates_an_invoice(macro_db, invoice, due, as_of, dtp, lateness, expected):
    got = macro_date(macro_db, invoice, due, as_of, dtp, lateness)
    assert got == D(expected)
    assert got == reference_date(D(invoice), D(due), D(as_of), dtp, lateness)


def test_a_shifted_date_is_never_beyond_91_days_after_as_of(macro_db):
    for offset in range(0, 14):
        as_of = AS_OF + timedelta(days=offset)
        for lateness in (0, 45, 90, 91, 200):
            got = macro_date(macro_db, "2025-12-01", "2026-01-01", as_of, 10, lateness)
            assert as_of < got <= as_of + timedelta(days=91)
            assert got in calendar()


def test_the_model_dates_cash_only_through_the_macro():
    model = (ROOT / "dbt" / "models" / "marts" / "fct_expected_cash.sql").read_text()
    code = "\n".join(re.sub(r"--[^\n]*", "", line) for line in model.splitlines())
    assert "expected_pay_date(" in code
    assert not re.search(r"\binvoice_date\b[^,;]*?\+|\bdate_add\s*\(", code, re.I)
