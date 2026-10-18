"""Customer name variants: the designed doubt band, no cross-customer match, determinism."""

from __future__ import annotations

import csv
import itertools
import re
import unicodedata
from collections import Counter

import pytest

from c2c.gen import plan as plan_mod
from c2c.gen.plan import build_plan, load_config
from c2c.gen.seeds import rng
from c2c.gen.variants import SYSTEMS, variants

ROOT = plan_mod.ROOT
AUTO_MIN, DOUBT_MIN = 60, 30
# A local copy of the scoring rules: points per rule fired, and the Jaccard threshold.
EMAIL_EXACT, DOMAIN, NAME_EQUAL, JACCARD, JACCARD_MIN_BPS = 60, 30, 40, 20, 8000


def _column(name, column):
    with (ROOT / "data" / "reference" / name).open(encoding="utf-8", newline="") as fh:
        return {row[column] for row in csv.DictReader(fh)}


SUFFIXES = _column("legal_suffixes.csv", "suffix")
FREEMAIL = _column("freemail_domains.csv", "domain")


def _tokens(name):
    text = re.sub(r"[\W_]+", " ", unicodedata.normalize("NFKC", name).lower())
    return frozenset(t for t in text.split() if t not in SUFFIXES)


def _domain(email):
    domain = email.rpartition("@")[2]
    return "" if domain in FREEMAIL else domain


def _records(plan, v):
    """(customer, system, source id, name, domain, billing emails) for every source record."""
    contacts = {}
    for row in v.crm_contacts:
        if row["role"] == "billing":
            contacts.setdefault(row["crm_account_id"], set()).add(row["email"])
    owner = {bid: acct for acct, bid in v.billing_customer_ids.items()}
    out = []
    for row in v.crm_accounts:
        acct = row["crm_account_id"]
        out.append((acct, "crm", acct, row["account_name"], row["website_domain"], contacts[acct]))
    for row in v.billing_customers:
        email = row["bill_to_email"]
        out.append((owner[row["billing_customer_id"]], "billing", row["billing_customer_id"],
                    row["bill_to_name"], _domain(email), {email}))
    for c in plan.customers:
        for system in ("gl", "bank"):
            out.append((c.crm_account_id, system, f"{system}:{c.crm_account_id}",
                        v.name_for(c, system), "", set()))
    return out


def _score(a, b):
    score = 0
    if a[5] & b[5]:
        score += EMAIL_EXACT
    if a[4] and a[4] == b[4]:
        score += DOMAIN
    ta, tb = _tokens(a[3]), _tokens(b[3])
    if ta == tb:
        score += NAME_EQUAL
    if ta | tb and 10_000 * len(ta & tb) // len(ta | tb) >= JACCARD_MIN_BPS:
        score += JACCARD
    return score


@pytest.fixture(scope="module")
def cfg():
    return load_config()


@pytest.fixture(scope="module")
def mini(cfg):
    plan = build_plan("mini", cfg, cfg["seed"])
    return plan, variants(plan, rng)


def _all_pairs(plan, v):
    records = _records(plan, v)
    return [(a, b, _score(a, b)) for a, b in itertools.combinations(records, 2)]


def _spellings(v, acct):
    return {v.name_for(acct, system) for system in SYSTEMS}


def test_exactly_one_customer_has_four_name_variants(mini):
    plan, v = mini
    counts = {c.crm_account_id: len(_spellings(v, c.crm_account_id)) for c in plan.customers}
    assert sorted(counts.values()).count(4) == 1
    assert counts == {c.crm_account_id: c.variant_count for c in plan.customers}
    (four,) = plan.roles["four_variant"]
    assert counts[four] == 4


def test_no_pair_across_two_customers_reaches_the_auto_match_score(mini):
    plan, v = mini
    worst = max(s for a, b, s in _all_pairs(plan, v) if a[0] != b[0])
    assert worst < AUTO_MIN


def test_exactly_one_doubt_pair_of_each_decision(mini):
    plan, v = mini
    doubt = [(a, b) for a, b, s in _all_pairs(plan, v) if DOUBT_MIN <= s < AUTO_MIN]
    decisions = Counter("same" if a[0] == b[0] else "different" for a, b in doubt)
    assert decisions == {"same": 1, "different": 1}

    designed = {d.decision: d for d in v.doubts}
    assert {d.decision for d in plan.doubt_pairs} == set(designed)
    for a, b in doubt:
        ids = {a[2], b[2]}
        d = designed["same" if a[0] == b[0] else "different"]
        assert ids == {d.crm_account_id, d.billing_customer_id}
        assert {a[0], b[0]} == ({d.customer} if d.decision == "same" else {d.customer, d.other})


def test_every_other_record_of_a_customer_scores_at_least_auto_match_against_its_crm_account(mini):
    plan, v = mini
    doubt_records = {d.billing_customer_id for d in v.doubts if d.decision == "same"}
    records = _records(plan, v)
    crm = {r[0]: r for r in records if r[1] == "crm"}
    for r in records:
        if r[1] == "crm" or r[2] in doubt_records:
            continue
        assert _score(r, crm[r[0]]) >= AUTO_MIN, r


def test_a_doubt_pair_shares_a_domain_with_a_mailbox_other_than_the_billing_contact(mini):
    plan, v = mini
    domains = {c.crm_account_id: c.domain for c in plan.customers}
    billing = {row["billing_customer_id"]: row for row in v.billing_customers}
    for d in v.doubts:
        email_domain = billing[d.billing_customer_id]["bill_to_email"].rpartition("@")[2]
        assert email_domain == domains[d.customer]
        if d.decision == "different":
            assert d.other != d.customer and domains[d.other] != domains[d.customer]
    contacts = {(r["crm_account_id"], r["role"]): r["email"] for r in v.crm_contacts}
    for d in v.doubts:
        assert billing[d.billing_customer_id]["bill_to_email"] != contacts[(d.customer, "billing")]


def test_contacts_cover_every_customer_and_some_use_free_mail(mini):
    plan, v = mini
    roles = Counter((r["crm_account_id"], r["role"]) for r in v.crm_contacts)
    assert set(roles.values()) == {1}
    assert {r for _, r in roles} == {"billing", "champion", "legal"}
    assert {a for a, _ in roles} == {c.crm_account_id for c in plan.customers}
    assert any(r["email"].rpartition("@")[2] in FREEMAIL for r in v.crm_contacts)


def test_rows_carry_the_source_columns_and_a_known_date(mini):
    plan, v = mini
    assert list(v.crm_accounts[0]) == [
        "crm_account_id", "account_name", "website_domain", "owner_rep_id", "created_on", "known_on"]
    assert list(v.crm_contacts[0]) == ["contact_id", "crm_account_id", "email", "role", "known_on"]
    assert list(v.billing_customers[0]) == [
        "billing_customer_id", "bill_to_name", "bill_to_email", "crm_account_ref", "known_on"]
    assert [r["crm_account_id"] for r in v.crm_accounts] == [c.crm_account_id for c in plan.customers]
    refs = {r["crm_account_ref"] for r in v.billing_customers}
    assert "" in refs and len(refs) > 1


def test_name_for_names_the_system_and_rejects_others(mini):
    plan, v = mini
    c = plan.customers[0]
    assert v.name_for(c, "crm") == v.crm_accounts[0]["account_name"]
    assert v.name_for(c.crm_account_id, "gl") == v.name_for(c, "gl")
    with pytest.raises(ValueError, match="unknown system"):
        v.name_for(c, "warehouse")


def test_two_calls_give_identical_output(cfg, mini):
    plan, first = mini
    assert variants(build_plan("mini", cfg, cfg["seed"]), rng) == first
    assert variants(plan, rng) == first


def test_a_different_seed_changes_the_spellings(cfg, mini):
    _, first = mini
    other = variants(build_plan("mini", cfg, cfg["seed"] + 1), rng)
    assert other.names != first.names


def test_full_preset_holds_the_same_guarantees(cfg):
    plan = build_plan("full", cfg, cfg["seed"])
    v = variants(plan, rng)
    pairs = _all_pairs(plan, v)
    assert max(s for a, b, s in pairs if a[0] != b[0]) < AUTO_MIN
    doubt = Counter("same" if a[0] == b[0] else "different"
                    for a, b, s in pairs if DOUBT_MIN <= s < AUTO_MIN)
    assert doubt == Counter(p.decision for p in plan.doubt_pairs)
    assert all(len(_spellings(v, c.crm_account_id)) == c.variant_count for c in plan.customers)
