"""The sign-off: a record that a named person reviewed the exact judgement files in force.

The reviewed-match file and the policy file are hashed; the sign-off is `<combined>.json` in the
sign-off directory, where `combined = sha256(matches + "\\n" + policy)`. Editing either file changes
the combined hash, so the sign-off no longer applies and `check` refuses with exit 4.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from .errors import C2CError, ControlFailure, PreconditionError, UsageError

CONTROL_ID = "CTL-SIGNOFF"
MATCHES_FILE = "customer_matches.yaml"
POLICY_FILE = "policy.yaml"
REMEDIATION = ("The files in force are not signed: review config/customer_matches.yaml and "
               "config/policy.yaml, then run `c2c signoff` in a terminal.")


class NotSigned(PreconditionError, ControlFailure):
    """The files in force are not signed. Exits 4 and, as a failed control, names CTL-SIGNOFF."""

    def __init__(self, message: str) -> None:
        C2CError.__init__(self, 4, message)
        self.failed_controls = [{"id": CONTROL_ID, "violating_rows": 1,
                                 "detail": {"row_hashes": [], "number_ids": []},
                                 "remediation": [REMEDIATION]}]


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def hashes(config_dir: str | Path) -> tuple[str, str, str]:
    """`(matches_sha256, policy_sha256, combined)` of the two files in `config_dir`."""
    config_dir = Path(config_dir)
    found = []
    for name in (MATCHES_FILE, POLICY_FILE):
        path = config_dir / name
        if not path.is_file():
            raise UsageError(f"{path} does not exist: the configuration directory must hold {MATCHES_FILE} "
                             f"and {POLICY_FILE}.")
        found.append(_sha256(path.read_bytes()))
    matches, policy = found
    return matches, policy, _sha256(f"{matches}\n{policy}".encode())


def _read(path: Path) -> dict | None:
    """A sign-off file's content, or None when it is not a readable sign-off record."""
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    keys = ("matches_sha256", "policy_sha256", "signer", "role", "signed_on")
    if not isinstance(record, dict) or not all(isinstance(record.get(k), str) and record[k] for k in keys):
        return None
    return record


def _records(signoff_dir: Path) -> list[tuple[Path, dict]]:
    if not signoff_dir.is_dir():
        return []
    read = ((path, _read(path)) for path in sorted(signoff_dir.glob("*.json")))
    return [(path, record) for path, record in read if record is not None]


def check(config_dir: str | Path, signoff_dir: str | Path) -> dict:
    """The sign-off record covering the files in `config_dir`; `NotSigned` (exit 4) when there is none.

    A file only counts when it is named for the combined hash and its own hashes recompute to that name,
    so renaming or editing a record does not make it apply to other files."""
    matches, policy, combined = hashes(config_dir)
    signoff_dir = Path(signoff_dir)
    records = _records(signoff_dir)
    for path, record in records:
        if path.name == f"{combined}.json" and (record["matches_sha256"], record["policy_sha256"]) == (matches, policy):
            return record
    # What the latest sign-off on file covers, for the message: latest by date, then by file name.
    latest = max(records, key=lambda item: (item[1]["signed_on"], item[0].name), default=None)
    signed_matches = latest[1]["matches_sha256"] if latest else "none"
    signed_policy = latest[1]["policy_sha256"] if latest else "none"
    raise NotSigned(f"not signed: matches expected {matches} signed {signed_matches}; "
                    f"policy expected {policy} signed {signed_policy}. No judgement-dependent step ran.")


def write_for(config_dir: str | Path, signoff_dir: str | Path, signer: str, role: str, signed_on: str) -> Path:
    """Write the sign-off for the files in `config_dir` into `signoff_dir`; returns the file written."""
    matches, policy, combined = hashes(config_dir)
    signoff_dir = Path(signoff_dir)
    signoff_dir.mkdir(parents=True, exist_ok=True)
    path = signoff_dir / f"{combined}.json"
    record = {"matches_sha256": matches, "policy_sha256": policy, "signer": signer, "role": role,
              "signed_on": signed_on}
    path.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
    return path
