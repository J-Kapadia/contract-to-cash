"""The seeded entity plan every generator stage reads.

`build_plan(preset, cfg, seed)` turns a preset of `config/generator.yaml` (read with
`load_config`) into a frozen `Plan`; the same preset, config values and seed give the same
plan whatever order the config keys are written in, and `Plan.to_json()` is its canonical
byte form. Stages take the plan and draw their own randomness from `seeds.rng(seed, entity,
purpose)`; they never decide who the customers are.

`Plan`
    preset, seed, company          the preset name, the run seed and the company name
    first_month, last_month        the books window (YYYY-MM)
    truth_first_month,             the truth horizon after the books; both None when the
    truth_last_month               preset has none
    months, truth_months           the months of the window and of the horizon
    calendar                       business days (YYYY-MM-DD) from the first books month to
                                   the last month of the horizon, from
                                   `data/reference/business_days.csv`
    customers                      `Customer`s in id order, `acct_0001`..
    reps                           `Rep`s, `rep_01`..
    doubt_pairs                    `DoubtPair`s, the pairs the matcher must be unsure about
    roles                          planted role -> account ids that carry it
    min_cash_hint                  placeholder minimum-cash figure in cents (mini only)
    headcount, vendors             the operating-cost schedules, copied from the config

`Customer`
    crm_account_id                 `acct_NNNN`, also the master customer id
    name, domain                   the true name and website domain (variants are derived
                                   from these by the variants stage)
    owner_rep_id, size_band, acv_cents
    signing_month, start_month     the contract is signed in one month and its term starts
                                   on the first day of the next
    term_months, billing_frequency (`annual` | `quarterly`), payment_terms_days
    renewal_count                  renewals that start inside the books window
    expansion_plan                 `Expansion(month, uplift_bps)` per renewal that expands,
                                   through the end of the truth horizon
    churn_month                    first month without service, "" when the customer stays
    dtp                            `DtpBehaviour(median_days, spread_days, late_share_bps,
                                   late_extra_days)`: how this customer pays
    variant_count                  distinct name spellings across CRM, billing, ledger, bank
    amendment_month                the mid-term amendment month, "" unless planted
    roles                          the planted roles this customer carries

Planted roles (only when the preset sets `planted_shapes`): `four_variant`, `amendment`,
`partial_payment`, `overpayment`, `multi_payment`, `stale_opportunity`, `doubt_pair_same`,
`doubt_pair_different` with its partner `doubt_lookalike`, and, when the preset names an
`inject_month`, one `inject_<error>` role per injected error (`INJECT_ERRORS`).

Choices made where the brief is silent: renewals keep the term length and billing cadence;
churn and expansion are decided at each term end, by renewal number (`first`, `later`);
doubt pairs alternate `same`, `different`; a `different` pair is two customers whose names
share their first word; ordinary customers carry 1 to `max_variants - 1` name variants.
"""

from __future__ import annotations

import csv
import dataclasses
import json
from dataclasses import dataclass
from pathlib import Path

import yaml

from .seeds import rng

ROOT = Path(__file__).resolve().parents[3]
REFERENCE_DIR = ROOT / "data" / "reference"
CONFIG_PATH = ROOT / "config" / "generator.yaml"

INJECT_ERRORS = (
    "duplicate_customer",
    "wrong_period",
    "misapplied_payment",
    "unbilled_amendment",
    "defrev_gl_slip",
    "orphan_bank_txn",
    "rounding_slip",
)
SHAPE_ROLES = (
    "four_variant",
    "amendment",
    "partial_payment",
    "overpayment",
    "multi_payment",
    "stale_opportunity",
)
FOUR_VARIANTS = 4
# Months after the start before a customer has paid an invoice inside a window.
SEASONED_MONTHS = 4

# Fixed synthetic word lists: a true name is first word, second word, legal suffix.
FIRST_WORDS = (
    "Alder", "Amber", "Aspen", "Basalt", "Birch", "Briar", "Cedar", "Cinder", "Cobalt", "Dawn",
    "Delta", "Dune", "Echo", "Elm", "Ember", "Fern", "Fjord", "Flint", "Garnet", "Glacier",
    "Grove", "Harbor", "Hazel", "Heron", "Indigo", "Iris", "Ivory", "Jasper", "Juniper", "Kelp",
    "Kestrel", "Larch", "Lotus", "Maple", "Meadow", "Nectar", "Nimbus", "Onyx", "Orchid", "Pebble",
    "Pine", "Quartz", "Quill", "Reed", "Rowan", "Sable", "Summit", "Thistle", "Tundra", "Umber",
    "Vale", "Willow", "Wren", "Yarrow", "Yew", "Zephyr", "Zinc", "Acorn", "Bramble", "Clover",
    "Dapple", "Eddy", "Fable", "Gale",
)
SECOND_WORDS = (
    "Analytics", "Apparel", "Aviation", "Biotech", "Brewing", "Capital", "Chemicals", "Clinics",
    "Construction", "Devices", "Dynamics", "Education", "Energy", "Foods", "Freight", "Furniture",
    "Gaming", "Glass", "Health", "Hospitality", "Insurance", "Logistics", "Marine", "Materials",
    "Media", "Mining", "Mobility", "Networks", "Optics", "Packaging", "Paper", "Pharma",
    "Publishing", "Rail", "Realty", "Retail", "Robotics", "Security", "Semiconductors", "Shipping",
    "Solar", "Staffing", "Steel", "Systems", "Telecom", "Textiles", "Utilities", "Water",
    "Wireless", "Agriculture", "Bakeries", "Cables", "Dairy", "Electronics", "Fabrics", "Grains",
    "Harvest", "Instruments", "Lighting", "Machinery",
)
LEGAL_SUFFIXES = ("Inc", "LLC", "Ltd", "Corp")
REP_NAMES = (
    "Avery Lindqvist", "Blair Okafor", "Casey Brandt", "Devon Castellan", "Emery Voss",
    "Finley Ardent",
)


@dataclass(frozen=True)
class Rep:
    rep_id: str
    name: str


@dataclass(frozen=True)
class DtpBehaviour:
    median_days: int
    spread_days: int
    late_share_bps: int
    late_extra_days: int


@dataclass(frozen=True)
class Expansion:
    month: str
    uplift_bps: int


@dataclass(frozen=True)
class Customer:
    crm_account_id: str
    name: str
    domain: str
    owner_rep_id: str
    size_band: str
    acv_cents: int
    signing_month: str
    start_month: str
    term_months: int
    billing_frequency: str
    payment_terms_days: int
    renewal_count: int
    expansion_plan: tuple[Expansion, ...]
    churn_month: str
    dtp: DtpBehaviour
    variant_count: int
    amendment_month: str
    roles: tuple[str, ...]


@dataclass(frozen=True)
class DoubtPair:
    pair_id: str
    decision: str  # the true answer: `same` (one customer's own variant) or `different`
    customer: str
    other: str  # the lookalike customer of a `different` pair, "" for `same`


@dataclass(frozen=True)
class Plan:
    preset: str
    seed: int
    company: str
    first_month: str
    last_month: str
    truth_first_month: str | None
    truth_last_month: str | None
    months: tuple[str, ...]
    truth_months: tuple[str, ...]
    calendar: tuple[str, ...]
    customers: tuple[Customer, ...]
    reps: tuple[Rep, ...]
    doubt_pairs: tuple[DoubtPair, ...]
    roles: dict[str, tuple[str, ...]]
    min_cash_hint: int | None
    headcount: dict
    vendors: tuple[dict, ...]

    def to_json(self) -> str:
        """Canonical form: sorted keys, no spaces, one trailing newline."""
        return json.dumps(dataclasses.asdict(self), sort_keys=True, separators=(",", ":")) + "\n"


# ---------------------------------------------------------------------------------- months

def _idx(month: str) -> int:
    year, mon = month.split("-")
    return int(year) * 12 + int(mon) - 1


def _month(idx: int) -> str:
    return f"{idx // 12:04d}-{idx % 12 + 1:02d}"


def month_range(first: str, last: str) -> tuple[str, ...]:
    return tuple(_month(i) for i in range(_idx(first), _idx(last) + 1))


def _quarter(month: str) -> int:
    return (int(month[5:]) - 1) // 3 + 1


# ---------------------------------------------------------------------------------- config

def load_config(path: str | Path = CONFIG_PATH) -> dict:
    """Read the generator config; a non-integer number anywhere in it is an error."""
    cfg = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    _reject_floats(cfg, "generator config")
    return cfg


def _reject_floats(node, where: str) -> None:
    if isinstance(node, float):
        raise ValueError(f"{where}: {node} is not an integer; write rates in basis points and money in cents")
    if isinstance(node, dict):
        for key, value in node.items():
            _reject_floats(value, f"{where}.{key}")
    elif isinstance(node, list):
        for i, value in enumerate(node):
            _reject_floats(value, f"{where}[{i}]")


def _weighted(r, weights: dict):
    """A key of `weights` drawn by integer weight; keys are taken in sorted order."""
    keys = sorted(weights)
    return r.choices(keys, [weights[k] for k in keys])[0]


def _between(r, bounds: dict) -> int:
    return r.randint(bounds["min"], bounds["max"])


# ------------------------------------------------------------------------------- customers

def _draft(i: int, p: dict, cfg: dict, seed: int, reps: list[Rep]) -> tuple[str, dict]:
    """A customer's fields before its name and planted roles are known."""
    acct = f"acct_{i:04d}"
    last = _idx(p["last_month"])
    horizon_last = _idx(p["truth_last_month"] or p["last_month"])

    span = month_range(p["signing_first_month"], _month(_idx(p["signing_first_month"]) + p["signing_span_months"] - 1))
    signing_month = rng(seed, acct, "signing").choices(span, [cfg["signing_weights"][_quarter(m)] for m in span])[0]
    start = _idx(signing_month) + 1
    if start > last:
        raise ValueError(f"{p['name']}: {acct} would start after the last books month")

    bands = cfg["size_bands"]
    size_band = _weighted(rng(seed, acct, "size"), {k: v["weight"] for k, v in bands.items()})
    step = cfg["acv_step_cents"]
    band = bands[size_band]
    acv_cents = rng(seed, acct, "acv").randint(-(-band["acv_min_cents"] // step), band["acv_max_cents"] // step) * step

    term_months = _weighted(rng(seed, acct, "term"), cfg["term_months_mix"])
    billing = _weighted(rng(seed, acct, "billing"), cfg["billing_frequency_mix"])
    terms = _weighted(rng(seed, acct, "payment_terms"), cfg["payment_terms_mix"])
    owner = rng(seed, acct, "rep").choice(reps).rep_id

    behaviour = cfg["dtp_behaviour"]
    r = rng(seed, acct, "dtp")
    dtp = DtpBehaviour(
        median_days=terms + _between(r, behaviour["median_offset_days"]),
        spread_days=_between(r, behaviour["spread_days"]),
        late_share_bps=_between(r, behaviour["late_share_bps"]),
        late_extra_days=_between(r, behaviour["late_extra_days"]),
    )

    renewal_count, expansions, churn_month = 0, [], ""
    k, end = 1, start + term_months
    while end <= horizon_last:
        cohort = "first" if k == 1 else "later"
        r = rng(seed, acct, f"renewal:{k}")
        churn_roll, expand_roll = r.randrange(10_000), r.randrange(10_000)
        uplift = _between(r, cfg["expansion_uplift_bps"])
        if churn_roll < cfg["churn_bps"][cohort]:
            churn_month = _month(end)
            break
        if end <= last:
            renewal_count += 1
        if expand_roll < cfg["expansion_bps"][cohort]:
            expansions.append(Expansion(_month(end), uplift))
        k, end = k + 1, end + term_months

    max_variants = p["max_variants"]
    variant_count = rng(seed, acct, "variants").randint(1, max(1, max_variants - 1))

    return acct, dict(
        owner_rep_id=owner, size_band=size_band, acv_cents=acv_cents,
        signing_month=signing_month, start_month=_month(start), term_months=term_months,
        billing_frequency=billing, payment_terms_days=terms, renewal_count=renewal_count,
        expansion_plan=tuple(expansions), churn_month=churn_month, dtp=dtp,
        variant_count=variant_count, amendment_month="", roles=(),
    )


def _live(f: dict, month: str) -> bool:
    """Whether the customer is in service in `month`."""
    return _idx(f["start_month"]) <= _idx(month) and (not f["churn_month"] or _idx(month) < _idx(f["churn_month"]))


def _invoiced(f: dict, month: str) -> bool:
    """Whether the customer's cadence has an invoice in `month`."""
    step = 12 if f["billing_frequency"] == "annual" else 3
    return _live(f, month) and (_idx(month) - _idx(f["start_month"])) % step == 0


def _amendment_bounds(f: dict, last_month: str) -> tuple[int, int]:
    """Inclusive month indexes where a mid-term amendment fits inside the first term and the books."""
    start = _idx(f["start_month"])
    return start + 2, min(start + f["term_months"] - 2, _idx(last_month))


def _pick(r, pool: list[str], prefer: list) -> str:
    """One of `pool`, narrowed by each preference in turn while it leaves someone."""
    for keep in prefer:
        narrowed = [a for a in pool if keep(a)]
        if narrowed:
            pool = narrowed
    return r.choice(pool)


def _names(seed: int, accts: list[str], lookalikes: dict[str, str]) -> dict[str, tuple[str, str, str]]:
    if len(accts) > min(len(FIRST_WORDS), len(SECOND_WORDS)):
        raise ValueError(f"{len(accts)} customers need more name words than the {len(FIRST_WORDS)} available")
    used_first, used_second, out = set(), set(), {}
    for acct in accts:
        r = rng(seed, acct, "name")
        while True:
            first, second = r.randrange(len(FIRST_WORDS)), r.randrange(len(SECOND_WORDS))
            if first not in used_first and second not in used_second:
                break
        used_first.add(first)
        used_second.add(second)
        out[acct] = (FIRST_WORDS[first], SECOND_WORDS[second], LEGAL_SUFFIXES[r.randrange(len(LEGAL_SUFFIXES))])
    for other, partner in sorted(lookalikes.items()):
        out[other] = (out[partner][0], out[other][1], out[other][2])
    return out


def _assign_roles(p: dict, seed: int, drafts: dict[str, dict]) -> tuple[list[DoubtPair], dict[str, list[str]]]:
    """Choose the doubt pairs and the planted roles; updates `drafts` in place."""
    accts = sorted(drafts)
    roles: dict[str, list[str]] = {}

    def give(role: str, acct: str) -> None:
        roles.setdefault(role, []).append(acct)
        drafts[acct]["roles"] = tuple(sorted(drafts[acct]["roles"] + (role,)))

    pairs: list[DoubtPair] = []
    n_pairs = p["doubt_pairs"]
    if n_pairs:
        if p["max_variants"] < 2:
            raise ValueError(f"{p['name']}: doubt pairs need max_variants of at least 2")
        order = list(accts)
        rng(seed, "plan", "doubt").shuffle(order)
        needed = sum(1 if i % 2 == 0 else 2 for i in range(n_pairs))
        if needed > len(order):
            raise ValueError(f"{p['name']}: {n_pairs} doubt pairs need {needed} customers, have {len(order)}")
        for i in range(n_pairs):
            if i % 2 == 0:
                acct = order.pop(0)
                pairs.append(DoubtPair(f"dp_{i + 1:02d}", "same", acct, ""))
                give("doubt_pair_same", acct)
                drafts[acct]["variant_count"] = max(drafts[acct]["variant_count"], 2)
            else:
                acct, other = order.pop(0), order.pop(0)
                pairs.append(DoubtPair(f"dp_{i + 1:02d}", "different", acct, other))
                give("doubt_pair_different", acct)
                give("doubt_lookalike", other)
    doubtful = {a for a in accts if drafts[a]["roles"]}

    if not p["planted_shapes"]:
        return pairs, roles
    if p["max_variants"] < FOUR_VARIANTS:
        raise ValueError(f"{p['name']}: planted shapes need max_variants of at least {FOUR_VARIANTS}")
    last = p["last_month"]
    seasoned = lambda a: _idx(drafts[a]["start_month"]) + SEASONED_MONTHS <= _idx(last) and (
        not drafts[a]["churn_month"] or _idx(drafts[a]["churn_month"]) > _idx(drafts[a]["start_month"]) + SEASONED_MONTHS)
    fits = lambda a: _amendment_bounds(drafts[a], last)[0] <= _amendment_bounds(drafts[a], last)[1]
    hard = {
        "amendment": fits,
        "partial_payment": seasoned,
        "overpayment": seasoned,
        "multi_payment": seasoned,
        "stale_opportunity": lambda a: _live(drafts[a], last),
    }
    held: set[str] = set()
    for role in SHAPE_ROLES:
        pool = [a for a in accts if hard.get(role, lambda _a: True)(a)]
        if not pool:
            raise ValueError(f"{p['name']}: no customer can carry the role {role}")
        prefer = [lambda a: a not in held, lambda a: a not in doubtful]
        if role == "multi_payment":
            prefer.append(lambda a: drafts[a]["billing_frequency"] == "quarterly")
        acct = _pick(rng(seed, "plan", f"role:{role}"), pool, prefer)
        held.add(acct)
        give(role, acct)
        if role == "four_variant":
            drafts[acct]["variant_count"] = FOUR_VARIANTS
        if role == "amendment":
            lo, hi = _amendment_bounds(drafts[acct], last)
            drafts[acct]["amendment_month"] = _month(rng(seed, acct, "amendment").randint(lo, hi))

    inject_month = p.get("inject_month")
    if inject_month:
        shaped = held | doubtful
        targeted: set[str] = set()
        for error in INJECT_ERRORS:
            pool = [a for a in accts if _live(drafts[a], inject_month)]
            if not pool:
                raise ValueError(f"{p['name']}: no customer is in service in {inject_month}")
            prefer = [lambda a: a not in targeted, lambda a: _invoiced(drafts[a], inject_month),
                      lambda a: a not in shaped]
            acct = _pick(rng(seed, "plan", f"role:inject_{error}"), pool, prefer)
            targeted.add(acct)
            give(f"inject_{error}", acct)
    return pairs, roles


def build_plan(preset: str, cfg: dict, seed: int, *, reference_dir: str | Path | None = None) -> Plan:
    """The plan for `preset` of the generator config `cfg` under `seed`."""
    if preset not in cfg["presets"]:
        raise ValueError(f"unknown preset {preset!r}; available: {', '.join(sorted(cfg['presets']))}")
    p = {**cfg["presets"][preset], "name": preset}

    reps = [Rep(f"rep_{i:02d}", REP_NAMES[(i - 1) % len(REP_NAMES)]) for i in range(1, p["reps"] + 1)]
    drafts = dict(_draft(i, p, cfg, seed, reps) for i in range(1, p["customers"] + 1))
    pairs, roles = _assign_roles(p, seed, drafts)

    lookalikes = {pair.other: pair.customer for pair in pairs if pair.other}
    names = _names(seed, sorted(drafts), lookalikes)
    customers = []
    for acct in sorted(drafts):
        first, second, suffix = names[acct]
        customers.append(Customer(
            crm_account_id=acct, name=f"{first} {second} {suffix}",
            domain=f"{first}{second}.example".lower(), **drafts[acct]))

    months = month_range(p["first_month"], p["last_month"])
    truth_first, truth_last = p["truth_first_month"], p["truth_last_month"]
    truth_months = month_range(truth_first, truth_last) if truth_first else ()
    calendar_last = truth_last or p["last_month"]
    reference = Path(reference_dir) if reference_dir else REFERENCE_DIR
    with (reference / "business_days.csv").open(encoding="utf-8", newline="") as fh:
        calendar = tuple(row["date"] for row in csv.DictReader(fh)
                         if row["is_business_day"] == "1" and p["first_month"] <= row["date"][:7] <= calendar_last)

    return Plan(
        preset=preset, seed=seed, company=cfg["company"],
        first_month=p["first_month"], last_month=p["last_month"],
        truth_first_month=truth_first, truth_last_month=truth_last,
        months=months, truth_months=truth_months, calendar=calendar,
        customers=tuple(customers), reps=tuple(reps), doubt_pairs=tuple(pairs),
        roles={role: tuple(sorted(accts)) for role, accts in sorted(roles.items())},
        min_cash_hint=p.get("min_cash_hint_cents"),
        headcount=json.loads(json.dumps(cfg["headcount"])),
        vendors=tuple(json.loads(json.dumps(v)) for v in cfg["vendors"]),
    )
