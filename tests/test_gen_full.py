"""The full books: generated inside the time budget, byte-identical, tied to the answer key.

Marked `slow`: each test writes the whole 50-customer, three-year dataset."""

from __future__ import annotations

import csv
import json
import time
from collections import Counter, defaultdict

import pytest

from c2c import gen
from c2c.gen import BOOKS, TRUTH, manifest
from c2c.gen.plan import build_plan, load_config
from c2c.gen.seeds import rng
from c2c.gen.truth import PLANTED
from c2c.gen.variants import variants

from conftest import ROOT
from test_gen_variants import AUTO_MIN, DOUBT_MIN, _all_pairs

pytestmark = pytest.mark.slow

SEED = 20261014
BUDGET_SECONDS = 30
FIRST_MONTH, LAST_MONTH = "2023-10", "2026-09"
HORIZON = ("2026-10-01", "2027-09-30")
DOUBT_PAIRS = range(10, 31)


def read(path):
    with path.open(encoding="utf-8", newline="") as fh:
        return list(csv.DictReader(fh))


def tree(root):
    return {p.relative_to(root).as_posix(): p.read_bytes() for p in sorted(root.rglob("*")) if p.is_file()}


def months(first, last):
    out = []
    while first <= last:
        out.append(first)
        year, mon = map(int, first.split("-"))
        first = f"{year + mon // 12:04d}-{mon % 12 + 1:02d}"
    return out


@pytest.fixture(scope="module")
def full(tmp_path_factory):
    out = tmp_path_factory.mktemp("full")
    started = time.monotonic()
    gen.generate("full", SEED, out)
    return out, time.monotonic() - started


def test_generation_fits_the_budget(full):
    _, seconds = full
    assert seconds <= BUDGET_SECONDS


def test_two_generations_are_byte_identical(full, tmp_path):
    out, _ = full
    gen.generate("full", SEED, tmp_path)
    assert tree(out) == tree(tmp_path)


def test_the_manifest_verifies_and_names_every_file(full):
    out, _ = full
    assert gen.verify(out, "full") == {"full": []}
    recorded = manifest.read(manifest.manifest_path(out, "full"))
    assert (recorded.preset, recorded.seed) == ("full", SEED)
    assert len(recorded.files) == len(BOOKS) + 1 + len(TRUTH) + 1
    assert set(recorded.files) == set(tree(out)) - {"books.manifest.sha256"}


def test_the_committed_manifest_matches_a_fresh_generation(full):
    out, _ = full
    committed = ROOT / "data" / "books.manifest.sha256"
    assert committed.read_text(encoding="utf-8") == manifest.manifest_path(out, "full").read_text(encoding="utf-8")


def test_about_fifty_customers_over_the_window_with_a_horizon_after_it(full):
    out, _ = full
    books_dir, truth_dir = manifest.preset_dirs(out, "full")
    assert json.loads((books_dir / "window.json").read_text(encoding="utf-8")) == {
        "first_month": FIRST_MONTH, "last_month": LAST_MONTH}
    assert len(read(books_dir / "crm_accounts.csv")) == 50

    posted = [r["gl_period"] for r in read(books_dir / "gl.csv")]
    assert min(posted) >= FIRST_MONTH and max(posted) <= LAST_MONTH
    assert max(r["posted_on"] for r in read(books_dir / "bank_transactions.csv")) <= f"{LAST_MONTH}-30"

    cash = read(truth_dir / "future_cash.csv")
    assert (cash[0]["date"], cash[-1]["date"]) == HORIZON
    invoices = read(truth_dir / "future_invoices.csv")
    assert invoices and HORIZON[0] <= min(r["invoice_date"] for r in invoices)
    assert max(r["invoice_date"] for r in invoices) <= HORIZON[1]
    assert {p.name for p in truth_dir.iterdir()} == {f"{stem}.csv" for stem in TRUTH} | {PLANTED}


def test_the_ledger_equals_the_expected_rollforward_every_month(full):
    out, _ = full
    books_dir, truth_dir = manifest.preset_dirs(out, "full")
    expected = defaultdict(lambda: defaultdict(int))
    for row in read(truth_dir / "expected_rollforward.csv"):
        for column, value in row.items():
            if column.endswith("_cents"):
                expected[row["month"]][column] += int(value)

    bank = read(books_dir / "bank_transactions.csv")
    cash = int(bank[0]["amount_cents"])
    disbursed = defaultdict(int)
    for row in bank[1:]:
        if int(row["amount_cents"]) < 0:
            disbursed[row["posted_on"][:7]] += -int(row["amount_cents"])
    ledger = defaultdict(lambda: defaultdict(int))
    for row in read(books_dir / "gl.csv"):
        ledger[row["gl_period"]][row["gl_account"]] += int(row["amount_cents"])

    balance = defaultdict(int)
    checked = 0
    for month in months(FIRST_MONTH, LAST_MONTH):
        for account, cents in ledger[month].items():
            balance[account] += cents
        cash += expected[month]["cash_applied_cents"] - disbursed[month]
        assert -balance["2400"] == expected[month]["defrev_closing_cents"], month
        assert balance["1200"] == expected[month]["ar_closing_cents"], month
        assert balance["1000"] == cash, month
        checked += 1
    assert checked == 36
    assert expected[LAST_MONTH]["ar_closing_cents"] > 0 and expected[LAST_MONTH]["defrev_closing_cents"] > 0


def test_the_doubt_band_is_neither_empty_nor_a_review_burden(full):
    out, _ = full
    _, truth_dir = manifest.preset_dirs(out, "full")
    cfg = load_config()
    plan = build_plan("full", cfg, SEED)
    pairs = _all_pairs(plan, variants(plan, rng))
    assert max(s for a, b, s in pairs if a[0] != b[0]) < AUTO_MIN
    doubt = [(a, b) for a, b, s in pairs if DOUBT_MIN <= s < AUTO_MIN]
    assert len(doubt) in DOUBT_PAIRS

    decisions = Counter("same" if a[0] == b[0] else "different" for a, b in doubt)
    expected = Counter(r["decision"] for r in read(truth_dir / "matches_expected.csv"))
    assert decisions == expected
