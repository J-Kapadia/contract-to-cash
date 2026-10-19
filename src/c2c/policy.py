"""The policy and the reviewed customer matches: the two committed files a sign-off covers.

`load` checks `policy.yaml` against the exact key set below: an unknown key, a missing key, a
value that is not an integer (a float, a bool, a string) or a negative one is a `PolicyError`
naming the key. Rates and amounts are integers (basis points, cents), never floats.
"""

from __future__ import annotations

import hashlib
import typing
from dataclasses import dataclass, fields, is_dataclass
from pathlib import Path

import yaml

from .errors import UsageError

POLICY_FILE = "policy.yaml"
MATCHES_FILE = "customer_matches.yaml"
CASE_NAMES = ("base", "downside", "upside")
DECISIONS = ("same", "different")
BPS_MAX = 10000


class PolicyError(UsageError):
    """A configuration file is missing, unreadable or does not match its schema (exit 2)."""


@dataclass(frozen=True)
class MatchWeights:
    email_exact: int
    domain: int
    name_equal: int
    jaccard: int
    remittance_ref: int


@dataclass(frozen=True)
class StageDefaults:
    S1: int
    S2: int
    S3: int
    S4: int
    S5: int


@dataclass(frozen=True)
class Case:
    renewal_down_bps: int
    renewal_up_bps: int
    expansion_mult_bps: int
    pipeline_weight_mult_bps: int
    pipeline_shift_days: int
    collections_percentile: int
    collections_cap_at_terms: int
    cloud_up_bps: int
    cloud_trailing_months: int


@dataclass(frozen=True)
class Cases:
    base: Case
    downside: Case
    upside: Case


@dataclass(frozen=True)
class Policy:
    auto_match_min: int
    doubt_min: int
    match_weights: MatchWeights
    jaccard_min_bps: int
    aging_buckets_days: tuple[int, ...]
    dtp_window_months: int
    dtp_min_paid_invoices: int
    min_observations: int
    at_risk_days_past_due: int
    stale_opp_shift_days: int
    stale_opp_weight_bps: int
    cohort_window_months: int
    stage_win_window_months: int
    stage_defaults_bps: StageDefaults
    cases: Cases
    min_cash_cents: int
    escalate_cents: int
    escalate_days_past_due: int
    renewal_review_days: int
    renewal_review_acv_percentile: int
    driver_review_bps: int
    backtest_min_history_months: int
    nrr_window_months: int


@dataclass(frozen=True)
class Match:
    a: str
    b: str
    decision: str
    reason: str


# Keys held to 0..10000 (a share), and keys held to 1..100 (a nearest-rank percentile).
_SHARES = {"jaccard_min_bps", "stale_opp_weight_bps", "driver_review_bps", "S1", "S2", "S3", "S4", "S5",
           "renewal_down_bps", "renewal_up_bps", "cloud_up_bps"}
_PERCENTILES = {"renewal_review_acv_percentile", "collections_percentile"}
_FLAGS = {"collections_cap_at_terms"}


def sha256_of(path: str | Path) -> str:
    """The hex SHA-256 of the file's bytes."""
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _read(path: Path) -> object:
    try:
        text = path.read_text()
    except OSError as e:
        raise PolicyError(f"cannot read {path}: {e.strerror or e}") from e
    try:
        return yaml.safe_load(text)
    except yaml.YAMLError as e:
        raise PolicyError(f"{path} is not valid YAML: {e}") from e


def _dotted(where: str, key: str) -> str:
    return f"{where}.{key}" if where else key


def _integer(value: object, key: str, where: str) -> int:
    name = _dotted(where, key)
    # bool is an int subclass; a YAML `true` is not a count.
    if isinstance(value, bool) or not isinstance(value, int):
        raise PolicyError(f"policy key {name} must be an integer, got {value!r}")
    if value < 0:
        raise PolicyError(f"policy key {name} must not be negative, got {value}")
    if key in _SHARES and value > BPS_MAX:
        raise PolicyError(f"policy key {name} must be at most {BPS_MAX} bps, got {value}")
    if key in _PERCENTILES and not 1 <= value <= 100:
        raise PolicyError(f"policy key {name} must be a percentile from 1 to 100, got {value}")
    if key in _FLAGS and value > 1:
        raise PolicyError(f"policy key {name} must be 0 or 1, got {value}")
    return value


def _build(cls: type, data: object, where: str = ""):
    """Build the frozen dataclass `cls` from a mapping with exactly its fields as keys."""
    label = where or "policy"
    if not isinstance(data, dict):
        raise PolicyError(f"policy key {label} must be a mapping, got {data!r}")
    wanted = [f.name for f in fields(cls)]
    for key in data:
        if key not in wanted:
            raise PolicyError(f"unknown policy key {_dotted(where, str(key))}")
    for key in wanted:
        if key not in data:
            raise PolicyError(f"missing policy key {_dotted(where, key)}")
    hints = typing.get_type_hints(cls)
    values = {}
    for key in wanted:
        hint, value = hints[key], data[key]
        if is_dataclass(hint):
            values[key] = _build(hint, value, _dotted(where, key))
        elif typing.get_origin(hint) is tuple:
            values[key] = _integers(value, key, where)
        else:
            values[key] = _integer(value, key, where)
    return cls(**values)


def _integers(value: object, key: str, where: str) -> tuple[int, ...]:
    name = _dotted(where, key)
    if not isinstance(value, list) or not value:
        raise PolicyError(f"policy key {name} must be a non-empty list of integers, got {value!r}")
    items = tuple(_integer(v, key, where) for v in value)
    if list(items) != sorted(set(items)):
        raise PolicyError(f"policy key {name} must be strictly ascending, got {list(items)}")
    return items


def load(config_dir: str | Path) -> Policy:
    """Read and check `<config_dir>/policy.yaml`."""
    data = _read(Path(config_dir) / POLICY_FILE)
    if isinstance(data, dict) and isinstance(data.get("cases"), dict):
        extra = [k for k in data["cases"] if k not in CASE_NAMES]
        if extra:
            raise PolicyError(f"unknown policy key cases.{extra[0]}: cases are {', '.join(CASE_NAMES)}")
    policy = _build(Policy, data)
    if policy.doubt_min > policy.auto_match_min:
        raise PolicyError(
            f"policy key doubt_min ({policy.doubt_min}) must not exceed auto_match_min ({policy.auto_match_min})")
    return policy


def load_matches(config_dir: str | Path) -> tuple[Match, ...]:
    """Read and check `<config_dir>/customer_matches.yaml`: `pairs: [{a, b, decision, reason}]`."""
    path = Path(config_dir) / MATCHES_FILE
    data = _read(path)
    if not isinstance(data, dict) or set(data) != {"pairs"}:
        raise PolicyError(f"{path} must hold exactly one key, pairs")
    pairs = data["pairs"]
    if not isinstance(pairs, list):
        raise PolicyError(f"{path}: pairs must be a list")
    matches: list[Match] = []
    seen: set[frozenset[str]] = set()
    for index, entry in enumerate(pairs, start=1):
        if not isinstance(entry, dict):
            raise PolicyError(f"{path}: pair {index} must be a mapping with a, b, decision and reason")
        for key in entry:
            if key not in ("a", "b", "decision", "reason"):
                raise PolicyError(f"{path}: pair {index} has unknown key {key}")
        for key in ("a", "b"):
            if not isinstance(entry.get(key), str) or not entry[key].strip():
                raise PolicyError(f"{path}: pair {index} needs a non-empty {key}")
        a, b = entry["a"], entry["b"]
        name = f"{a} / {b}"
        if a == b:
            raise PolicyError(f"{path}: pair {name} pairs a customer with itself")
        decision = entry.get("decision")
        if decision not in DECISIONS:
            raise PolicyError(
                f"{path}: pair {name} has decision {decision!r}; set it to same or different")
        reason = entry.get("reason")
        if not isinstance(reason, str) or not reason.strip():
            raise PolicyError(f"{path}: pair {name} needs a one-line reason")
        if frozenset((a, b)) in seen:
            raise PolicyError(f"{path}: pair {name} is listed twice")
        seen.add(frozenset((a, b)))
        matches.append(Match(a=a, b=b, decision=decision, reason=reason.strip()))
    return tuple(matches)
