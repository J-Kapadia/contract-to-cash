"""The grammar of number ids: which ids exist, and how one splits into its parts.

An id is `/`-separated. Its first segment names a family; the second is the month (`YYYY-MM`) or
the as-of date (`YYYY-MM-DD`) it belongs to, except for `master`, `ref`, `cfg` and the backtest
summary, which belong to no month. The set of families and of every metric, line and bucket is
closed: an id outside it does not parse, and adding a family or a metric is a change to this module.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from typing import Callable

MONTH = re.compile(r"\d{4}-(0[1-9]|1[0-2])")
AS_OF = re.compile(r"\d{4}-\d{2}-\d{2}")
# `*` stands for all customers pooled, which only the days-to-pay family uses.
CUSTOMER = re.compile(r"acct_\d{4,}")
BUCKET = re.compile(r"current|\d+-\d+|\d+\+")
WEEK = re.compile(r"W(0[1-9]|1[0-3])")
KEY = re.compile(r"[^\s/\x00-\x1f\x7f]+")
CONFIG_KEY = re.compile(r"[A-Za-z0-9_]+(\.[A-Za-z0-9_]+)*")

CASES = ("base", "downside", "upside")
GL_ACCOUNTS = ("1000", "1200", "2400", "3000", "4000", "4100", "6000", "6100", "6200", "6300", "6400", "6500")
CUST_METRICS = ("members", "arr")
LINE_TYPES = ("subscription", "overage", "credit")
DEFREV_METRICS = ("opening", "billings", "recognised", "closing")
AR_METRICS = ("opening", "billed", "collected", "credits", "closing")
CASH_METRICS = ("opening", "receipts", "disbursements", "closing")
DTP_METRICS = ("median", "p80")
REVENUE_SOURCES = ("contracted", "renewal", "expansion", "new")
CASH_LINES = ("open_ar", "contracted", "renewal", "expansion", "new",
              "payroll", "commissions", "cloud", "vendors", "events", "rent")
BALANCES = ("opening", "ending")
RATE_COHORTS = ("first", "later")
WIN_STAGES = ("S1", "S2", "S3", "S4", "S5")
BACKTEST_METRICS = ("cash_in_wape_bps", "net_cash_error_cents", "ending_balance_error_cents",
                    "baseline_wape_bps", "revenue_3m_error_bps")
BRIDGE_LINES = ("revenue", "defrev_movement", "net_billings", "ar_movement", "receipts")
CHG_METRICS = ("arr_new", "arr_expansion", "arr_contraction", "arr_churn")
ROLLUP_METRICS = ("nrr_bps", "grr_bps", "logo_churn", "stale_opps")
MASTER_METRICS = ("precision_auto", "recall_auto", "pairs_doubt", "pairs_reviewed")
DRIFT_CATEGORIES = ("new_account", "new_customer", "new_line_type", "volume_shift")
DRIFT_COUNTS = ("new_accounts", "new_customers", "new_line_types", "volume_shifts")
DSO_METRICS = ("ar_closing", "billed", "days_in_month", "days")
RECURRING_LINES = ("recurring", "non_recurring", "total")
CONCENTRATION_TOPS = ("top1", "top5", "top10")
CONCENTRATION_CUSTOMER = ("customer", "share")
REF_FILES = ("business_days", "customer_matches", "freemail_domains", "gl_accounts", "legal_suffixes")


@dataclass(frozen=True)
class ParsedId:
    """A number id split into its family, the month or as-of date it belongs to (None for ids that
    belong to none) and the segments after that, in order."""
    family: str
    month_or_as_of: str | None
    parts: tuple[str, ...]


def _month(value: str) -> bool:
    return MONTH.fullmatch(value) is not None


def _as_of(value: str) -> bool:
    if AS_OF.fullmatch(value) is None:
        return False
    try:
        date.fromisoformat(value)
    except ValueError:
        return False
    return True


def _customer(value: str) -> bool:
    return CUSTOMER.fullmatch(value) is not None


def _one_of(*choices: str) -> Callable[[str], bool]:
    return lambda value: value in choices


def _matching(pattern: re.Pattern[str]) -> Callable[[str], bool]:
    return lambda value: pattern.fullmatch(value) is not None


def _pooled_customer(value: str) -> bool:
    return value == "*" or _customer(value)


def _forecast(rest: tuple[str, ...]) -> bool:
    """What follows `fc/<as_of>`: `<case>/` and a revenue, cash or alert number."""
    if not rest or rest[0] not in CASES:
        return False
    tail = rest[1:]
    if tail[:1] == ("rev",):
        return len(tail) == 3 and _month(tail[1]) and tail[2] in REVENUE_SOURCES
    if tail[:1] == ("cash",):
        return len(tail) == 3 and WEEK.fullmatch(tail[1]) is not None and tail[2] in CASH_LINES + BALANCES
    return tail == ("alert", "first_breach_week")


def _rate(rest: tuple[str, ...]) -> bool:
    """What follows `rate/<as_of>`: a renewal or expansion cohort, or a win stage."""
    if len(rest) != 2:
        return False
    kind, cohort_or_stage = rest
    return cohort_or_stage in (WIN_STAGES if kind == "win" else RATE_COHORTS if kind in ("renewal", "expansion") else ())


def _aging(rest: tuple[str, ...]) -> bool:
    """What follows `aging/<month>`: a bucket total, or a customer's bucket."""
    if len(rest) == 1:
        return BUCKET.fullmatch(rest[0]) is not None
    return len(rest) == 2 and _customer(rest[0]) and BUCKET.fullmatch(rest[1]) is not None


def _drift(rest: tuple[str, ...]) -> bool:
    """What follows `drift/<month>`: a category and one key, or the count of a category's items."""
    if len(rest) == 1:
        return rest[0] in DRIFT_COUNTS
    return len(rest) == 2 and rest[0] in DRIFT_CATEGORIES and KEY.fullmatch(rest[1]) is not None


def _cutoff(rest: tuple[str, ...]) -> bool:
    """What follows `cutoff/<month>`: the count of findings, or one finding's invoice and ledger period."""
    if len(rest) == 1:
        return rest[0] == "findings"
    return len(rest) == 2 and KEY.fullmatch(rest[0]) is not None and _month(rest[1])


def _concentration(rest: tuple[str, ...]) -> bool:
    """What follows `concentration/<month>`: a top-N share, or a customer's revenue or share."""
    if len(rest) == 1:
        return rest[0] in CONCENTRATION_TOPS
    return len(rest) == 2 and rest[0] in CONCENTRATION_CUSTOMER and _customer(rest[1])


def _books_file(value: str) -> bool:
    from .gen import BOOKS  # the generator is heavy: only a `schema` id needs the stems

    return value in BOOKS


def _shape(*checks: Callable[[str], bool]) -> Callable[[tuple[str, ...]], bool]:
    """A rule that the remaining segments are exactly as many as `checks`, each passing its own."""
    return lambda rest: len(rest) == len(checks) and all(check(part) for check, part in zip(checks, rest))


# family -> (what the second segment must be, or None when the family has no month; rule for the rest)
_FAMILIES: dict[str, tuple[Callable[[str], bool] | None, Callable[[tuple[str, ...]], bool]]] = {
    "cust": (_month, _shape(_customer, _one_of(*CUST_METRICS))),
    "rev": (_month, _shape(_customer, _one_of(*LINE_TYPES))),
    "defrev": (_month, _shape(_customer, _one_of(*DEFREV_METRICS))),
    "ar": (_month, _shape(_customer, _one_of(*AR_METRICS))),
    "cash": (_month, _shape(_one_of(*CASH_METRICS))),
    "tie": (_month, _shape(_one_of(*GL_ACCOUNTS))),
    "aging": (_month, _aging),
    "dtp": (_month, _shape(_pooled_customer, _one_of(*DTP_METRICS))),
    "fc": (_as_of, _forecast),
    "rate": (_as_of, _rate),
    "bridge": (_month, _shape(_one_of(*BRIDGE_LINES))),
    "chg": (_month, _shape(_one_of(*CHG_METRICS))),
    "rollup": (_month, _shape(_one_of(*ROLLUP_METRICS))),
    "drift": (_month, _drift),
    "dso": (_month, _shape(_one_of(*DSO_METRICS))),
    "recurring": (_month, _shape(_one_of(*RECURRING_LINES))),
    "cutoff": (_month, _cutoff),
    "concentration": (_month, _concentration),
    "schema": (_month, _shape(_books_file)),
    "master": (None, _shape(_one_of(*MASTER_METRICS))),
    "ref": (None, _shape(_one_of(*REF_FILES), _matching(KEY))),
    "cfg": (None, _shape(_matching(CONFIG_KEY))),
}
BACKTEST_CASE_METRIC = _shape(_one_of(*CASES), _one_of(*BACKTEST_METRICS))
FAMILY_NAMES = tuple(_FAMILIES) + ("bt",)


def parse(number_id: str) -> ParsedId:
    """Split `number_id` into its family, month or as-of date, and remaining segments.

    Raises ValueError, naming the id, when it is not in the grammar."""
    parsed = _parse(tuple(number_id.split("/"))) if isinstance(number_id, str) else None
    if parsed is None:
        raise ValueError(f"{number_id!r} is not a number id: it is outside the closed set of families and metrics.")
    return parsed


def _parse(segments: tuple[str, ...]) -> ParsedId | None:
    family, rest = segments[0], segments[1:]
    if family == "bt":
        # `bt/<as_of>/<case>/<metric>` per as-of, `bt/summary/<case>/<metric>` over all of them.
        if rest[:1] == ("summary",):
            return ParsedId(family, None, rest) if BACKTEST_CASE_METRIC(rest[1:]) else None
        if rest and _as_of(rest[0]) and BACKTEST_CASE_METRIC(rest[1:]):
            return ParsedId(family, rest[0], rest[1:])
        return None
    rule = _FAMILIES.get(family)
    if rule is None:
        return None
    check_period, check_rest = rule
    if check_period is None:
        return ParsedId(family, None, rest) if check_rest(rest) else None
    if rest and check_period(rest[0]) and check_rest(rest[1:]):
        return ParsedId(family, rest[0], rest[1:])
    return None


def is_valid(number_id: str) -> bool:
    """Whether `number_id` is in the grammar."""
    try:
        parse(number_id)
    except ValueError:
        return False
    return True
