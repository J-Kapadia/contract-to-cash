"""The volume-shift threshold is a policy key: both committed files carry it, and the loader checks it."""

from __future__ import annotations

from pathlib import Path

import duckdb
import pytest
import yaml

from c2c import policy
from c2c.load import load_config

ROOT = Path(__file__).resolve().parents[1]
FILES = {"default": ROOT / "config", "fixture": ROOT / "tests" / "fixtures" / "config"}
KEY = "drift_volume_shift_bps"


def copy_with(tmp_path: Path, source: Path, **changes: object) -> Path:
    document = yaml.safe_load((source / "policy.yaml").read_text())
    document.update(changes)
    (tmp_path / "policy.yaml").write_text(yaml.safe_dump(document))
    return tmp_path


@pytest.mark.parametrize("name", FILES)
def test_the_committed_policy_files_carry_the_threshold(name):
    assert policy.load(FILES[name]).drift_volume_shift_bps == 5000


@pytest.mark.parametrize("name", FILES)
def test_a_threshold_above_a_whole_share_is_rejected_naming_the_key(name, tmp_path):
    with pytest.raises(policy.PolicyError) as caught:
        policy.load(copy_with(tmp_path, FILES[name], **{KEY: 10001}))
    assert KEY in str(caught.value)
    assert caught.value.exit == 2


def test_a_policy_without_the_threshold_is_rejected_as_missing(tmp_path):
    source = FILES["default"]
    document = yaml.safe_load((source / "policy.yaml").read_text())
    del document[KEY]
    (tmp_path / "policy.yaml").write_text(yaml.safe_dump(document))
    with pytest.raises(policy.PolicyError) as caught:
        policy.load(tmp_path)
    assert f"missing policy key {KEY}" in str(caught.value)


def test_the_loaded_configuration_holds_the_threshold_in_the_warehouse():
    con = duckdb.connect(":memory:")
    load_config(con, FILES["fixture"])
    rows = con.execute("SELECT value FROM raw.ref_policy WHERE key = ?", [KEY]).fetchall()
    assert rows == [(5000,)]
