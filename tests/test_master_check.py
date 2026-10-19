"""`c2c master --check`: pairwise precision and recall against the answer key, for the automatic
matcher alone and after review, and the rollup rows it writes."""

from __future__ import annotations

import csv
import shutil
from pathlib import Path

from c2c import db
from conftest import DEFAULT_CONFIG, MINI_BOOKS, MINI_TRUTH, c2c

MONTH = "2026-03"


def run(tmp_path: Path, *extra: str, config: tuple[Path, Path] | None = None):
    env = {"C2C_CONFIG_DIR": str(config[0]), "C2C_SIGNOFF_DIR": str(config[1])} if config else None
    # the harness points C2C_BOOKS at a private root, which would win over the fixture's books
    return c2c("master", "--month", MONTH, "--fixture", "mini", "--books", str(MINI_BOOKS),
               "--warehouse", str(tmp_path / "w.duckdb"), *extra, "--json", env=env)


def rollup(tmp_path: Path) -> dict[str, tuple[int, str]]:
    con = db.connect(tmp_path / "w.duckdb", read_only=True)
    try:
        rows = con.execute("SELECT metric, value_cents, value_text FROM mart.fct_rollup WHERE month = ?",
                           [MONTH]).fetchall()
    finally:
        con.close()
    return {metric: (cents, text) for metric, cents, text in rows}


def test_mini_scores_10000_after_review_and_for_the_automatic_matcher(tmp_path):
    done = run(tmp_path, "--check")
    assert done.code == 0, done.stderr
    scores = done.json["scores"]
    assert scores["precision_reviewed"] == scores["recall_reviewed"] == 10000
    assert scores["precision_auto"] == 10000
    assert scores["pairs_doubt"] == scores["pairs_reviewed"] == 2
    assert {c["id"] for c in done.json["controls"]} >= {"CTL-MASTER-REVIEW", "CTL-MASTER-ANCHOR"}


def test_the_report_lists_every_pair_with_its_evidence_and_decision(tmp_path):
    done = run(tmp_path)
    assert done.code == 0, done.stderr
    assert "scores" not in done.json
    pairs = done.json["candidates"]
    con = db.connect(tmp_path / "w.duckdb", read_only=True)
    try:
        assert len(pairs) == con.execute("SELECT count(*) FROM mart.fct_match_candidates").fetchone()[0]
    finally:
        con.close()
    for pair in pairs:
        assert set(pair) == {"a", "b", "score", "rules_fired", "decision"}
        assert pair["decision"] != "auto" or pair["rules_fired"]
    reviewed = {(p["a"]["id"], p["b"]["id"]): p["decision"] for p in pairs if p["decision"] in ("same", "different")}
    assert reviewed == {("bill_0003", "acct_0007"): "different", ("bill_0009", "acct_0009"): "same"}
    members = [(m["system"], m["id"]) for cluster in done.json["clusters"] for m in cluster]
    assert len(members) == len(set(members))


def test_check_writes_the_master_rows_of_the_rollup(tmp_path):
    assert run(tmp_path).code == 0
    assert not any(metric.startswith("master/") for metric in rollup(tmp_path))
    assert run(tmp_path, "--check").code == 0
    assert run(tmp_path, "--check").code == 0  # a second run replaces the rows
    assert rollup(tmp_path) == {
        "master/precision_auto": (10000, "10000 bps"), "master/recall_auto": (10000, "10000 bps"),
        "master/pairs_doubt": (2, "2 pairs"), "master/pairs_reviewed": (2, "2 pairs")}


def test_check_without_the_answer_key_exits_4(tmp_path):
    empty = tmp_path / "no-truth"
    empty.mkdir()
    done = run(tmp_path, "--truth", str(empty), "--check")
    assert done.code == 4
    assert "answer key" in done.json["message"]
    assert not (tmp_path / "w.duckdb").exists()
    # without --check the report needs no answer key
    assert run(tmp_path, "--truth", str(empty)).code == 0


def test_an_unsigned_configuration_exits_4_before_the_warehouse_is_opened(tmp_path):
    config = tmp_path / "config"
    shutil.copytree(DEFAULT_CONFIG, config)
    done = run(tmp_path, config=(config, tmp_path / "no-signoff"))
    assert done.code == 4
    assert not (tmp_path / "w.duckdb").exists()


# ---------------------------------------------------------------- a review the answer key disagrees with

def truth_merging(tmp_path: Path, billing_id: str, account_id: str) -> Path:
    """A copy of the answer key in which the customer behind `billing_id` is the one behind `account_id`."""
    truth = tmp_path / "truth"
    shutil.copytree(MINI_TRUTH, truth)
    with (truth / "identities.csv").open(encoding="utf-8", newline="") as fh:
        rows = list(csv.DictReader(fh))
    owner = {(r["source_system"], r["source_id"]): r["true_customer_id"] for r in rows}
    gone, kept = owner[("billing", billing_id)], owner[("crm", account_id)]
    assert gone != kept
    for row in rows:
        if row["true_customer_id"] == gone:
            row["true_customer_id"] = kept
    with (truth / "identities.csv").open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(rows[0]), lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)
    return truth


def test_a_different_decision_on_a_true_same_pair_costs_recall_and_exits_5(tmp_path):
    # the reviewed matches record bill_0003 and acct_0007 as different; here the answer key says they are one customer
    truth = truth_merging(tmp_path, "bill_0003", "acct_0007")
    done = run(tmp_path, "--truth", str(truth), "--check")
    assert done.code == 5
    assert "recall after review" in done.json["message"]
    assert "precision" not in done.json["message"]
    rows = rollup(tmp_path)
    assert rows["master/precision_auto"][0] == 10000  # the rows are written before the check fails
    assert rows["master/recall_auto"][0] < 10000
