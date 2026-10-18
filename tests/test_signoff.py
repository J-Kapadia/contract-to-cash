"""The sign-off: stable hashes, a file named for the combined hash, and no signing without a person."""

from __future__ import annotations

import hashlib
import json
import os
import pty
import shutil
import subprocess
import sys

import pytest

from c2c import signoff
from c2c.errors import PreconditionError, UsageError

from conftest import ROOT, SIGNED_ON, c2c, signed_config


def test_hashes_are_stable_and_combine_the_two_file_hashes():
    first = signoff.hashes(ROOT / "config")
    assert signoff.hashes(ROOT / "config") == first
    matches, policy, combined = first
    assert matches == hashlib.sha256((ROOT / "config" / "customer_matches.yaml").read_bytes()).hexdigest()
    assert policy == hashlib.sha256((ROOT / "config" / "policy.yaml").read_bytes()).hexdigest()
    assert combined == hashlib.sha256(f"{matches}\n{policy}".encode()).hexdigest()


def test_the_file_is_named_for_the_combined_hash_and_records_the_signature(tmp_path):
    config_dir, signoff_dir = signed_config(tmp_path)
    matches, policy, combined = signoff.hashes(config_dir)
    [written] = signoff_dir.glob("*.json")
    assert written.name == f"{combined}.json"
    assert json.loads(written.read_text()) == {
        "matches_sha256": matches, "policy_sha256": policy,
        "signer": "Test signer (synthetic)", "role": "finance_lead", "signed_on": SIGNED_ON}
    assert signoff.check(config_dir, signoff_dir)["matches_sha256"] == matches


def test_a_record_renamed_for_other_files_does_not_apply(tmp_path):
    config_dir, signoff_dir = signed_config(tmp_path)
    (config_dir / "policy.yaml").write_text((config_dir / "policy.yaml").read_text() + "\n# edited\n")
    _, _, combined = signoff.hashes(config_dir)
    [written] = signoff_dir.glob("*.json")
    shutil.copy(written, signoff_dir / f"{combined}.json")  # same content, now named for the new files
    with pytest.raises(PreconditionError):
        signoff.check(config_dir, signoff_dir)


def test_check_without_a_signoff_says_none_and_names_the_control(tmp_path):
    config_dir, _ = signed_config(tmp_path)
    matches, policy, _ = signoff.hashes(config_dir)
    with pytest.raises(PreconditionError) as raised:
        signoff.check(config_dir, tmp_path / "nowhere")
    assert raised.value.exit == 4
    assert raised.value.message == (f"not signed: matches expected {matches} signed none; "
                                    f"policy expected {policy} signed none. No judgement-dependent step ran.")
    assert [c["id"] for c in raised.value.failed_controls] == ["CTL-SIGNOFF"]


def test_a_missing_config_file_is_a_usage_error(tmp_path):
    with pytest.raises(UsageError):
        signoff.hashes(tmp_path)


def test_the_command_refuses_without_a_terminal(tmp_path):
    config_dir, signoff_dir = signed_config(tmp_path)
    before = sorted(signoff_dir.iterdir())
    done = c2c("signoff", "--signer", "Someone", "--role", "finance_lead", "--config", str(config_dir),
               "--json", env={"C2C_SIGNOFF_DIR": str(tmp_path / "other")})
    assert done.code == 4, done.stderr
    assert done.json["exit"] == 4 and "terminal" in done.json["message"]
    assert not (tmp_path / "other").exists()
    assert sorted(signoff_dir.iterdir()) == before


def _signoff_in_a_terminal(config_dir, signoff_dir, answer: bytes) -> int:
    master, slave = pty.openpty()
    env = {**{k: v for k, v in os.environ.items() if not k.startswith("C2C_")}, "C2C_SIGNOFF_DIR": str(signoff_dir)}
    proc = subprocess.Popen([sys.executable, "-m", "c2c", "signoff", "--signer", "A. Reviewer", "--role",
                             "finance_lead", "--config", str(config_dir)],
                            cwd=ROOT, env=env, stdin=slave, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    os.close(slave)
    try:
        os.write(master, answer)
        return proc.wait(timeout=60)
    finally:
        os.close(master)


def test_in_a_terminal_a_yes_signs_and_a_no_does_not(tmp_path):
    config_dir, _ = signed_config(tmp_path)
    (config_dir / "customer_matches.yaml").write_text("pairs: []\n# reviewed\n")
    signoff_dir = tmp_path / "fresh"

    assert _signoff_in_a_terminal(config_dir, signoff_dir, b"n\n") != 0
    assert not signoff_dir.exists() or not list(signoff_dir.glob("*.json"))

    assert _signoff_in_a_terminal(config_dir, signoff_dir, b"y\n") == 0
    record = signoff.check(config_dir, signoff_dir)
    assert (record["signer"], record["role"]) == ("A. Reviewer", "finance_lead")
