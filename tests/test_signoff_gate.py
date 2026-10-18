"""A close needs a sign-off of the configuration in force, and without one writes nothing."""

from __future__ import annotations

import hashlib
import shutil

from c2c import signoff

from conftest import CLOSED_MONTH, c2c, signed_config


def _digest(path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _close(closed, tmp_path, *extra, env=None):
    """Close the shared month against a copy of its warehouse; returns the result and the copy."""
    warehouse = tmp_path / "w.duckdb"
    shutil.copy(closed.warehouse, warehouse)
    done = c2c("close", "--month", CLOSED_MONTH, "--books", str(closed.books), "--warehouse", str(warehouse),
               "--json", *extra, env=env)
    return done, warehouse


def test_a_close_without_a_signoff_exits_4_and_changes_no_mart_table(closed, tmp_path):
    config_dir, _ = signed_config(tmp_path)
    matches, policy, _ = signoff.hashes(config_dir)
    warehouse = tmp_path / "w.duckdb"
    shutil.copy(closed.warehouse, warehouse)
    before = _digest(warehouse)

    done = c2c("close", "--month", CLOSED_MONTH, "--books", str(closed.books), "--warehouse", str(warehouse),
               "--config", str(config_dir), "--json", env={"C2C_SIGNOFF_DIR": str(tmp_path / "no-signoff")})

    assert done.code == 4, done.stderr
    assert [c["id"] for c in done.json["failed_controls"]] == ["CTL-SIGNOFF"]
    assert done.json["message"] == (f"not signed: matches expected {matches} signed none; "
                                    f"policy expected {policy} signed none. No judgement-dependent step ran.")
    assert _digest(warehouse) == before


def test_editing_the_matches_after_signing_exits_4_with_both_hashes(closed, tmp_path):
    config_dir, signoff_dir = signed_config(tmp_path)
    signed_matches, signed_policy, _ = signoff.hashes(config_dir)
    matches_file = config_dir / "customer_matches.yaml"
    matches_file.write_text(matches_file.read_text() + "# reviewed again\n")
    new_matches, _, _ = signoff.hashes(config_dir)
    warehouse = tmp_path / "w.duckdb"
    shutil.copy(closed.warehouse, warehouse)
    before = _digest(warehouse)

    done = c2c("close", "--month", CLOSED_MONTH, "--books", str(closed.books), "--warehouse", str(warehouse),
               "--config", str(config_dir), "--json", env={"C2C_SIGNOFF_DIR": str(signoff_dir)})

    assert done.code == 4, done.stderr
    assert [c["id"] for c in done.json["failed_controls"]] == ["CTL-SIGNOFF"]
    assert done.json["message"] == (f"not signed: matches expected {new_matches} signed {signed_matches}; "
                                    f"policy expected {signed_policy} signed {signed_policy}. "
                                    "No judgement-dependent step ran.")
    assert _digest(warehouse) == before


def test_a_signed_copy_of_the_config_closes(closed, tmp_path):
    config_dir, signoff_dir = signed_config(tmp_path)
    done, _ = _close(closed, tmp_path, "--config", str(config_dir), env={"C2C_SIGNOFF_DIR": str(signoff_dir)})
    assert done.code == 0, done.stderr


def test_the_suite_signs_the_default_config_so_a_plain_close_passes(closed, tmp_path):
    done, _ = _close(closed, tmp_path)
    assert done.code == 0, done.stderr
