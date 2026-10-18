"""Customer name variants across CRM, billing, ledger and bank.

`variants(plan, rng)` turns the entity plan into the customer-facing source rows: CRM
accounts and contacts, and billing customers. It also decides which spelling of each
customer's name the ledger and bank stages write (`Variants.name_for`). The module is pure:
the plan and the seeded streams (`rng(seed, entity, purpose)`, as in `seeds.rng`) give the
same rows every time.

Every customer has `variant_count` distinct spellings of its name, spread over four systems:
the CRM account keeps the true name; the billing customer, the ledger subledger and the bank
counterparty text may each carry another spelling. Spellings come in two families:

    same after normalising      suffix swaps, dropped suffix, punctuation, upper and lower case
    different after normalising typos and abbreviations of the second word

Matching scores (name equal 40, token Jaccard 20, billing contact email 60, domain 30) then
give, by construction:

    - the ledger and bank records are name-only and always normalise to the CRM name, so each
      scores 60 against the CRM account;
    - a billing customer scores 60 or more against its CRM account, through the billing
      contact email (some contacts are on free-mail domains) or a name that normalises equal;
    - a `same` doubt pair is one customer whose only billing customer carries a typo of the
      name and a mailbox on the customer's own domain, other than the billing contact: the
      shared domain alone scores 30 against the CRM account;
    - a `different` doubt pair is two customers whose names share a first word, where the
      lookalike's billing mailbox sits on the other customer's domain: 30 against that
      customer's CRM account. The other customer's own billing contact is on a free-mail
      domain, so no further pair shares the domain;
    - no two records of different customers share a name after normalising, and none shares
      anything but that one domain.

Choices made where the brief is silent: a customer has one billing customer, and the `same`
doubt pair is that customer's only one; `known_on` of a CRM account and its contacts is the
account's `created_on` (the month before the contract is signed), and a billing customer is
known on the first day of the month its term starts; `crm_account_ref` is filled for about a
third of the customers with no doubt role; the spellings per customer are bounded by the
three systems that can differ from the CRM, so at most four.
"""

from __future__ import annotations

import csv
import re
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from functools import cache
from random import Random

from .plan import REFERENCE_DIR, Customer, Plan
from .seeds import rng as default_rng

SYSTEMS = ("crm", "billing", "gl", "bank")
# Token Jaccard at which a non-matching spelling would start to score: stay below it.
JACCARD_MIN_BPS = 8000
FREEMAIL_SHARE_BPS = 1500
CRM_REF_SHARE_BPS = 3000

GIVEN_NAMES = (
    "Ada", "Bruno", "Chloe", "Dmitri", "Elena", "Farid", "Greta", "Hugo", "Ines", "Jonas",
    "Katya", "Liam",
)
FAMILY_NAMES = (
    "Almeida", "Brandt", "Castillo", "Dubois", "Eriksen", "Fontaine", "Gallagher", "Hartmann",
    "Ito", "Janssen", "Kowalski", "Lindgren",
)
SUFFIX_SWAPS = ("Inc", "Incorporated", "LLC", "Ltd", "Limited", "Corp", "Corporation", "Co.")
ROLES = ("billing", "champion", "legal")

Rng = Callable[[int, str, str], Random]


@dataclass(frozen=True)
class DoubtRecord:
    """The two source records of a designed doubt pair, with the true answer."""

    pair_id: str
    decision: str  # `same` or `different`
    customer: str
    other: str  # the lookalike customer of a `different` pair, "" for `same`
    crm_account_id: str
    billing_customer_id: str


@dataclass(frozen=True)
class Variants:
    """Source rows (dicts keyed by column name, in file column order) and the name spellings."""

    crm_accounts: tuple[dict, ...]
    crm_contacts: tuple[dict, ...]
    billing_customers: tuple[dict, ...]
    names: dict[str, dict[str, str]]  # customer id -> system -> spelling
    billing_customer_ids: dict[str, str]  # customer id -> its billing customer id
    doubts: tuple[DoubtRecord, ...]

    def name_for(self, customer: Customer | str, system: str) -> str:
        """The spelling of the customer's name in `system` (`crm`, `billing`, `gl` or `bank`)."""
        if system not in SYSTEMS:
            raise ValueError(f"unknown system {system!r}; expected one of {', '.join(SYSTEMS)}")
        acct = customer if isinstance(customer, str) else customer.crm_account_id
        return self.names[acct][system]


# ---------------------------------------------------------------------------- normalising

@cache
def _legal_suffixes() -> frozenset[str]:
    with (REFERENCE_DIR / "legal_suffixes.csv").open(encoding="utf-8", newline="") as fh:
        return frozenset(row["suffix"] for row in csv.DictReader(fh))


@cache
def _freemail_domains() -> tuple[str, ...]:
    with (REFERENCE_DIR / "freemail_domains.csv").open(encoding="utf-8", newline="") as fh:
        return tuple(row["domain"] for row in csv.DictReader(fh))


def _tokens(name: str) -> frozenset[str]:
    """Lowercase, NFKC, punctuation to space, legal suffixes removed."""
    text = re.sub(r"[\W_]+", " ", unicodedata.normalize("NFKC", name).lower())
    return frozenset(t for t in text.split() if t not in _legal_suffixes())


def _jaccard_bps(a: frozenset[str], b: frozenset[str]) -> int:
    return 10_000 * len(a & b) // len(a | b) if a | b else 0


# ----------------------------------------------------------------------------- spellings

def _dedupe(names: list[str], true_name: str) -> list[str]:
    seen, out = {true_name}, []
    for name in names:
        if name not in seen:
            seen.add(name)
            out.append(name)
    return out


def _same_after_normalising(name: str) -> list[str]:
    first, second, suffix = name.split()
    base = f"{first} {second}"
    swaps = [f"{base} {s}" for s in SUFFIX_SWAPS if s != suffix]
    others = [
        base, f"{base}, {suffix}.", f"{base} {suffix}.", f"{first}-{second} {suffix}",
        f"{base}, {suffix}", name.upper(), name.lower(), f"{base.upper()}, {suffix.upper()}.",
    ]
    want = _tokens(name)
    return [c for c in _dedupe(swaps + others, name) if _tokens(c) == want]


def _different_after_normalising(name: str) -> list[str]:
    first, second, suffix = name.split()
    words = [second[:i] + second[i + 1:] for i in range(1, len(second) - 1)]
    words += [second[:i] + second[i + 1] + second[i] + second[i + 2:] for i in range(1, len(second) - 2)]
    words.append(second[:max(3, len(second) - 3)] + ".")
    want = _tokens(name)
    out = []
    for word in words:
        candidate = f"{first} {word} {suffix}"
        if _tokens(candidate) != want and _jaccard_bps(_tokens(candidate), want) < JACCARD_MIN_BPS:
            out.append(candidate)
    return _dedupe(out, name)


def _spellings(
    plan: Plan, c: Customer, rng_: Rng, *, doubt_same: bool, lookalike: bool,
) -> dict[str, str]:
    """The spelling each system uses for `c`; `variant_count` distinct strings in all."""
    out = {system: c.name for system in SYSTEMS}
    extras = c.variant_count - 1
    if extras <= 0:
        return out
    order = rng_(plan.seed, c.crm_account_id, "variants:slots")
    slots = ["billing"] + order.sample(["gl", "bank"], 2) if doubt_same else order.sample(["billing", "gl", "bank"], 3)
    if extras > len(slots):
        raise ValueError(f"{c.crm_account_id}: {c.variant_count} name spellings do not fit in {len(SYSTEMS)} systems")
    used = {c.name}
    for slot in slots[:extras]:
        if doubt_same:
            pool = _different_after_normalising(c.name) if slot == "billing" else _same_after_normalising(c.name)
        elif slot == "billing" and not lookalike:
            pool = _same_after_normalising(c.name) + _different_after_normalising(c.name)
        else:
            pool = _same_after_normalising(c.name)
        rng_(plan.seed, c.crm_account_id, f"variants:spelling:{slot}").shuffle(pool)
        pick = next((p for p in pool if p not in used), None)
        if pick is None:
            raise ValueError(f"{c.crm_account_id}: no unused spelling left for {slot}")
        used.add(pick)
        out[slot] = pick
    return out


# -------------------------------------------------------------------------------- rows

def _created_on(c: Customer, r: Random) -> str:
    year, month = map(int, c.signing_month.split("-"))
    year, month = (year, month - 1) if month > 1 else (year - 1, 12)
    return date(year, month, r.randint(1, 28)).isoformat()


def variants(plan: Plan, rng: Rng = default_rng) -> Variants:
    """The source rows and name spellings for every customer of `plan`."""
    by_id = {c.crm_account_id: c for c in plan.customers}
    doubt_same = {p.customer for p in plan.doubt_pairs if p.decision == "same"}
    different_of = {p.other: p.customer for p in plan.doubt_pairs if p.decision == "different"}
    freemail_forced = {p.customer for p in plan.doubt_pairs if p.decision == "different"}

    names: dict[str, dict[str, str]] = {}
    crm_accounts, crm_contacts, billing_customers = [], [], []
    billing_ids: dict[str, str] = {}

    for c in plan.customers:
        acct = c.crm_account_id
        num = acct.split("_")[1]
        names[acct] = _spellings(plan, c, rng, doubt_same=acct in doubt_same, lookalike=acct in different_of)

        created_on = _created_on(c, rng(plan.seed, acct, "variants:created"))
        crm_accounts.append({
            "crm_account_id": acct, "account_name": names[acct]["crm"], "website_domain": c.domain,
            "owner_rep_id": c.owner_rep_id, "created_on": created_on, "known_on": created_on,
        })

        r = rng(plan.seed, acct, "variants:contacts")
        people = [(r.choice(GIVEN_NAMES), r.choice(FAMILY_NAMES)) for _ in ROLES]
        free = acct in freemail_forced or r.randrange(10_000) < FREEMAIL_SHARE_BPS
        emails = {}
        for role, (given, family) in zip(ROLES, people):
            local = f"{given}.{family}".lower()
            if role == "billing" and free:
                emails[role] = f"{local}{num}@{r.choice(_freemail_domains())}"
            else:
                emails[role] = f"{local}@{c.domain}"
        for i, role in enumerate(ROLES, start=1):
            crm_contacts.append({
                "contact_id": f"contact_{num}_{i}", "crm_account_id": acct, "email": emails[role],
                "role": role, "known_on": created_on,
            })

        if acct in doubt_same:
            bill_email = f"accounts@{c.domain}"
        elif acct in different_of:
            bill_email = f"accounts.payable@{by_id[different_of[acct]].domain}"
        else:
            bill_email = emails["billing"]
        in_doubt = acct in doubt_same or acct in different_of or acct in freemail_forced
        ref = acct if not in_doubt and rng(plan.seed, acct, "variants:ref").randrange(10_000) < CRM_REF_SHARE_BPS else ""
        billing_ids[acct] = f"bill_{num}"
        billing_customers.append({
            "billing_customer_id": billing_ids[acct], "bill_to_name": names[acct]["billing"],
            "bill_to_email": bill_email, "crm_account_ref": ref, "known_on": f"{c.start_month}-01",
        })

    doubts = tuple(
        DoubtRecord(
            p.pair_id, p.decision, p.customer, p.other, p.customer,
            billing_ids[p.customer if p.decision == "same" else p.other],
        )
        for p in plan.doubt_pairs
    )
    return Variants(
        crm_accounts=tuple(crm_accounts), crm_contacts=tuple(crm_contacts),
        billing_customers=tuple(billing_customers), names=names,
        billing_customer_ids=billing_ids, doubts=doubts,
    )
