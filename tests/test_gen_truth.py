"""The answer key written beside the books: roll-forwards the ledger must equal, every name variant,
the planted problems, and the future that is truth only."""

from __future__ import annotations

import csv
import json
import re
from collections import defaultdict
from datetime import date, timedelta

import pytest

from c2c import gen
from c2c.gen.plan import INJECT_ERRORS
from c2c.gen.truth import FILES, PLANTED, planted_problems

SEED = 1
INJECT_MONTH = "2026-01"
WINDOW = ("2024-10", "2026-03")


def read(path):
    with path.open(encoding="utf-8", newline="") as fh:
        return list(csv.DictReader(fh))


def months(first, last):
    out = []
    while first <= last:
        out.append(first)
        year, mon = map(int, first.split("-"))
        first = f"{year + mon // 12:04d}-{mon % 12 + 1:02d}"
    return out


@pytest.fixture(scope="module")
def mini(tmp_path_factory):
    out = tmp_path_factory.mktemp("mini")
    gen.generate("mini", SEED, out)
    books, truth = out / "books", out / "truth"
    return (
        {p.stem: read(p) for p in books.glob("*.csv")},
        {p.stem: read(p) for p in truth.glob("*.csv")},
        json.loads((truth / PLANTED).read_text(encoding="utf-8")),
    )


def test_every_truth_file_is_written(tmp_path):
    for preset in ("small", "mini"):
        out = tmp_path / preset
        gen.generate(preset, SEED, out)
        truth_dir = gen.manifest.preset_dirs(out, preset)[1]
        truth_prefix = truth_dir.relative_to(out).as_posix()
        assert {p.name for p in truth_dir.iterdir()} == {f"{s}.csv" for s in FILES} | {PLANTED}
        assert set(gen.manifest.read(gen.manifest.manifest_path(out, preset)).files) >= {
            f"{truth_prefix}/{s}.csv" for s in FILES} | {f"{truth_prefix}/{PLANTED}"}


def test_the_ledger_equals_the_expected_rollforward_every_month(mini):
    books, truth, _ = mini
    expected = defaultdict(lambda: defaultdict(int))
    for row in truth["expected_rollforward"]:
        for column, value in row.items():
            if column.endswith("_cents"):
                expected[row["month"]][column] += int(value)

    opening = int(books["bank_transactions"][0]["amount_cents"])
    disbursed = defaultdict(int)
    for row in books["bank_transactions"][1:]:
        if int(row["amount_cents"]) < 0:
            disbursed[row["posted_on"][:7]] += -int(row["amount_cents"])
    ledger = defaultdict(lambda: defaultdict(int))
    for row in books["gl"]:
        ledger[row["gl_period"]][row["gl_account"]] += int(row["amount_cents"])

    checked, cash = 0, opening
    balance = defaultdict(int)
    for month in months(*WINDOW):
        for account, cents in ledger[month].items():
            balance[account] += cents
        cash += expected[month]["cash_applied_cents"] - disbursed[month]
        assert -balance["2400"] == expected[month]["defrev_closing_cents"], month
        assert balance["1200"] == expected[month]["ar_closing_cents"], month
        assert balance["1000"] == cash, month
        checked += 1
    assert checked == 18
    assert expected[WINDOW[1]]["ar_closing_cents"] > 0 and expected[WINDOW[1]]["defrev_closing_cents"] > 0


def test_each_rollforward_opens_where_the_last_closed(mini):
    _, truth, _ = mini
    last = {}
    for row in truth["expected_rollforward"]:
        for kind in ("defrev", "ar"):
            opening, closing = int(row[f"{kind}_opening_cents"]), int(row[f"{kind}_closing_cents"])
            assert opening == last.get((row["customer_id"], kind), 0)
            assert closing == (opening + int(row[f"{kind}_additions_cents"]) - int(row[f"{kind}_reductions_cents"]))
            last[row["customer_id"], kind] = closing
    seen = {r["month"] for r in truth["expected_rollforward"]}
    assert seen == set(months(min(seen), WINDOW[1])) and min(seen) >= WINDOW[0]


def test_every_variant_in_the_books_is_an_identity(mini):
    books, truth, _ = mini
    true_id = {(r["source_system"], r["source_id"]): r["true_customer_id"] for r in truth["identities"]}
    assert len(true_id) == len(truth["identities"])

    billing_owner = {r["billing_customer_id"]: true_id["billing", r["billing_customer_id"]]
                     for r in books["billing_customers"]}
    for row in books["crm_accounts"]:
        assert true_id["crm", row["crm_account_id"]] == row["crm_account_id"]
        assert true_id["crm_name", row["account_name"]] == row["crm_account_id"]
    for row in books["billing_customers"]:
        assert true_id["billing_name", row["bill_to_name"]] == billing_owner[row["billing_customer_id"]]

    ledger_names = {r["subledger_name"] for r in books["gl"] if r["gl_account"] in ("1200", "2400")}
    assert ledger_names and all(("gl_name", name) in true_id for name in ledger_names)
    receipts = [r for r in books["bank_transactions"][1:] if int(r["amount_cents"]) > 0]
    assert receipts and all(("bank_name", r["counterparty_text"]) in true_id for r in receipts)

    payer = {p["bank_txn_id"]: billing_owner[p["billing_customer_id"]] for p in books["payments"]}
    assert all(true_id["bank_name", r["counterparty_text"]] == payer[r["bank_txn_id"]] for r in receipts)
    assert {t for t in true_id.values()} == {r["crm_account_id"] for r in books["crm_accounts"]}


def test_the_planted_problems_name_all_seven_errors(mini):
    books, _, planted = mini
    assert set(planted["errors"]) == set(INJECT_ERRORS) and len(INJECT_ERRORS) == 7
    assert planted["inject_month"] == INJECT_MONTH and planted["seed"] == SEED
    for error, target in planted["errors"].items():
        rows = [r for r in books[target["table"]] if all(str(r[k]) == str(v) for k, v in target["key"].items())]
        assert rows, error
        assert target["month"] == INJECT_MONTH
        month_of = {"invoices": "invoice_date", "payments": "applied_on", "bank_transactions": "posted_on",
                    "gl": "gl_period"}.get(target["table"])
        if month_of:
            assert all(r[month_of][:7] == INJECT_MONTH for r in rows), error


def test_the_target_is_the_errors_own_pick_from_its_customers_rows(mini):
    _, _, planted = mini
    plan = gen.build_plan("mini", gen.load_config(), SEED)
    books, _ = gen._stages(plan)
    assert planted_problems(plan, books, INJECT_MONTH) == planted
    assert planted_problems(plan, books, None)["errors"] == {}
    holders = [e for e, t in planted["errors"].items() if t["customer"] in plan.roles[f"inject_{e}"]]
    assert len(holders) >= 4


def test_future_rows_start_the_day_after_the_books(mini):
    books, truth, _ = mini
    cash = truth["future_cash"]
    days = [date.fromisoformat(r["date"]) for r in cash]
    assert days[0] == date(2026, 4, 1) and days[-1] == date(2026, 6, 30)
    assert all(b - a == timedelta(days=1) for a, b in zip(days, days[1:]))

    invoices = truth["future_invoices"]
    assert invoices and min(r["invoice_date"] for r in invoices) > "2026-03-31"
    assert max(r["invoice_date"] for r in invoices) <= "2026-06-30"
    assert {r["invoice_id"] for r in invoices}.isdisjoint(r["invoice_id"] for r in books["invoices"])
    books_last = max(int(r["invoice_id"].split("_")[1]) for r in books["invoices"])
    assert min(int(r["invoice_id"].split("_")[1]) for r in invoices) == books_last + 1
    assert all(r["known_on"] == r["invoice_date"] for r in invoices)

    assert max(r["posted_on"] for r in books["bank_transactions"]) <= "2026-03-31"


def test_future_cash_rolls_from_the_books_closing_balance(mini):
    books, truth, _ = mini
    balance = sum(int(r["amount_cents"]) for r in books["bank_transactions"])
    for row in truth["future_cash"]:
        net = int(row["receipts_cents"]) - int(row["disbursements_cents"])
        assert int(row["net_cents"]) == net
        balance += net
        assert int(row["closing_cents"]) == balance
    assert sum(int(r["receipts_cents"]) for r in truth["future_cash"]) > 0
    assert sum(int(r["disbursements_cents"]) for r in truth["future_cash"]) > 0


def test_the_stages_run_on_past_the_window_without_clashing_with_the_books(mini):
    books, _, _ = mini
    # Rebuild the future rows from the same plan to see the ids that continue the books'.
    plan = gen.build_plan("mini", gen.load_config(), SEED)
    future = gen._future(plan, books)
    ids = [r["bank_txn_id"] for r in future["bank_transactions"]]
    assert len(ids) == len(set(ids)) and not set(ids) & {r["bank_txn_id"] for r in books["bank_transactions"]}
    opex_ids = sorted(int(i.split("_")[2]) for i in ids if i.startswith("bk_opx_"))
    books_opex = max(int(r["bank_txn_id"].split("_")[2]) for r in books["bank_transactions"]
                     if r["bank_txn_id"].startswith("bk_opx_"))
    assert opex_ids == list(range(books_opex + 1, books_opex + 1 + len(opex_ids)))
    receipts = sorted(int(m.group(1)) for i in ids if (m := re.fullmatch(r"bk_(\d+)", i)))
    last = max(int(m.group(1)) for r in books["bank_transactions"]
               if (m := re.fullmatch(r"bk_(\d+)", r["bank_txn_id"])))
    assert receipts == list(range(last + 1, last + 1 + len(receipts)))
    assert all(r["posted_on"] > "2026-03-31" for r in future["bank_transactions"])


def test_the_books_do_not_depend_on_the_horizon(tmp_path):
    gen.generate("mini", SEED, tmp_path / "a")
    config = gen.load_config()
    config["presets"]["mini"]["truth_first_month"] = config["presets"]["mini"]["truth_last_month"] = None
    plan = gen.build_plan("mini", config, SEED)
    books, _ = gen._stages(plan)
    assert len(books["invoices"]) == len(read(tmp_path / "a" / "books" / "invoices.csv"))
    assert gen._future(plan, books) == {"invoices": [], "bank_transactions": []}


def test_doubt_pairs_carry_their_true_decision(mini):
    _, truth, _ = mini
    rows = truth["matches_expected"]
    assert sorted(r["decision"] for r in rows) == ["different", "same"]
    assert all(r["source_a"].startswith("acct_") and r["source_b"].startswith("bill_") for r in rows)


def test_renewal_outcomes_and_behaviour_cover_the_customers(mini):
    books, truth, _ = mini
    assert {r["customer_id"] for r in truth["collection_behaviour"]} == {r["crm_account_id"] for r in books["crm_accounts"]}
    assert truth["renewal_outcomes"]


def test_a_preset_without_a_horizon_has_empty_future_files(tmp_path):
    gen.generate("small", SEED, tmp_path)
    truth = tmp_path / "truth" / "small"
    for stem in ("future_invoices", "future_cash", "matches_expected"):
        assert read(truth / f"{stem}.csv") == []
    assert json.loads((truth / PLANTED).read_text(encoding="utf-8"))["errors"] == {}
    assert read(truth / "expected_rollforward.csv")


def test_truth_is_byte_identical_across_runs(tmp_path):
    gen.generate("mini", SEED, tmp_path / "a")
    gen.generate("mini", SEED, tmp_path / "b")
    names = {p.name for p in (tmp_path / "a" / "truth").iterdir()}
    assert names
    for name in names:
        assert (tmp_path / "a" / "truth" / name).read_bytes() == (tmp_path / "b" / "truth" / name).read_bytes()
