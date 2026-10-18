"""CTL-MASTER-REVIEW and CTL-MASTER-ANCHOR: each passes on the clean `mini` books and fails the close,
naming the cluster or pair in its remediation, on a copy with the problem planted. A reviewed pair
whose customer names are gone is a warning in the result, never a failure."""

from __future__ import annotations

import csv
import shutil
from pathlib import Path

import yaml
from conftest import DEFAULT_CONFIG, signed_config

MONTH = "2026-01"
REVIEW, ANCHOR = "CTL-MASTER-REVIEW", "CTL-MASTER-ANCHOR"
NEW_BILLING = "bill_0011"  # a second billing customer for one of the existing customers
LONELY_BILLING = "bill_0004"  # re-named so that it matches no CRM account


def read(books: Path, name: str) -> list[dict]:
    with (books / name).open(encoding="utf-8", newline="") as fh:
        return list(csv.DictReader(fh))


def write(books: Path, name: str, rows: list[dict]) -> None:
    with (books / name).open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def close_january(workspace, config: tuple[Path, Path] | None = None):
    env = {"C2C_CONFIG_DIR": str(config[0]), "C2C_SIGNOFF_DIR": str(config[1])} if config else None
    done = workspace.run("close", "--month", MONTH, "--json", env=env)
    return done, {entry["id"]: entry for entry in done.json.get("failed_controls", [])}


def signed_matches(tmp_path: Path, pairs: list[dict]) -> tuple[Path, Path]:
    """The `mini` configuration with `pairs` as its reviewed matches, signed."""
    source = tmp_path / "source"
    shutil.copytree(DEFAULT_CONFIG, source)
    matches = source / "customer_matches.yaml"
    matches.write_text(yaml.safe_dump({"pairs": pairs}), encoding="utf-8")
    return signed_config(tmp_path / "signed", source)


def reviewed_pairs() -> list[dict]:
    return yaml.safe_load((DEFAULT_CONFIG / "customer_matches.yaml").read_text(encoding="utf-8"))["pairs"]


def test_the_clean_mini_passes_both_controls_and_has_no_stale_review(mini_books_copy, workspace):
    done = workspace.run("close", "--month", MONTH, "--json")
    assert done.code == 0, done.stderr
    controls = {entry["id"]: entry for entry in done.json["controls"]}
    assert controls[REVIEW] == {"id": REVIEW, "status": "pass", "violating_rows": 0}
    assert controls[ANCHOR] == {"id": ANCHOR, "status": "pass", "violating_rows": 0}
    assert done.json["stale_review_pairs"] == []


def test_a_second_billing_customer_in_the_doubt_band_fails_the_close_naming_the_pair(mini_books_copy, workspace):
    customers = read(mini_books_copy, "billing_customers.csv")
    [existing] = [row for row in customers if row["billing_customer_id"] == "bill_0010"]
    domain = existing["bill_to_email"].split("@")[1]
    customers.append(dict(existing, billing_customer_id=NEW_BILLING, bill_to_name="Pinewood Textiles Ltd",
                          bill_to_email=f"billing@{domain}", crm_account_ref=""))
    write(mini_books_copy, "billing_customers.csv", customers)

    done, failed = close_january(workspace)

    assert done.code == 3, done.stderr
    review = failed[REVIEW]
    assert review["violating_rows"] == 2  # the new customer against the CRM account and against the old one
    assert all(NEW_BILLING in step and "(score 30)" in step and "{" not in step for step in review["remediation"])
    assert any("acct_0010" in step for step in review["remediation"])
    assert any("customer_matches.yaml" in step and "c2c signoff" in step for step in review["remediation"])
    # until it is reviewed the new customer is a cluster of its own, which has no CRM account
    assert NEW_BILLING in failed[ANCHOR]["remediation"][0]


def test_a_cluster_without_a_crm_account_fails_the_close_naming_its_members(mini_books_copy, workspace):
    customers = read(mini_books_copy, "billing_customers.csv")
    [lonely] = [row for row in customers if row["billing_customer_id"] == LONELY_BILLING]
    lonely.update(bill_to_name="Quasar Holdings GmbH", bill_to_email="ap@quasar-holdings.example",
                  crm_account_ref="")
    write(mini_books_copy, "billing_customers.csv", customers)

    done, failed = close_january(workspace)

    assert done.code == 3, done.stderr
    anchor = failed[ANCHOR]
    assert anchor["violating_rows"] == 1
    [remediation] = anchor["remediation"]
    assert LONELY_BILLING in remediation and "has 0 CRM accounts" in remediation and "{" not in remediation
    assert REVIEW not in failed


def test_a_reviewed_pair_for_a_deleted_variant_is_a_warning_not_a_failure(mini_books_copy, workspace, tmp_path):
    ghost = {"a": "acct_0003", "b": "bill_0099", "decision": "same", "reason": "the variant was deleted"}
    config = signed_matches(tmp_path, reviewed_pairs() + [ghost])

    done, failed = close_january(workspace, config)

    assert done.code == 0, done.stderr
    assert failed == {}
    assert done.json["stale_review_pairs"] == [{"a": "acct_0003", "b": "bill_0099", "decision": "same"}]
    assert "1 reviewed pair" in done.json["message"]
