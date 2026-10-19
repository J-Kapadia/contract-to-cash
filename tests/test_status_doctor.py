"""`c2c status` lists the months and forecast runs; `c2c doctor` grades the installation 0, 1 or 2."""

from __future__ import annotations

import json
import shutil

import jsonschema
import pytest

from c2c import db, signoff, state

from conftest import MINI_BOOKS, MINI_TRUTH, ROOT, c2c, shared_dir, signed_config

ENVELOPE_SCHEMA = json.loads((ROOT / "src/c2c/schemas/envelope.schema.json").read_text())
MONTH = "2026-03"


@pytest.fixture(scope="session")
def mini_closed(tmp_path_factory):
    """The `mini` books with one month closed, built once; tests copy the warehouse."""

    def build(root):
        done = c2c("close", "--month", MONTH, "--books", str(MINI_BOOKS), "--warehouse", str(root / "w.duckdb"),
                   "--json")
        assert done.code == 0, done.stderr

    return shared_dir(tmp_path_factory, "closed-mini", build) / "w.duckdb"


@pytest.fixture
def warehouse(mini_closed, tmp_path):
    """A private copy of the closed warehouse with the month recorded as closed."""
    copy = tmp_path / "w.duckdb"
    shutil.copy(mini_closed, copy)
    with db.connect(copy) as con:
        state.transition(con, MONTH, state.CLOSED, state.input_digest(con, MONTH), "0" * 64)
    return copy


def status(warehouse, *args):
    return c2c("status", "--warehouse", str(warehouse), *args)


def doctor(warehouse, *args, env=None):
    return c2c("doctor", "--books", str(MINI_BOOKS), "--truth", str(MINI_TRUTH), "--warehouse", str(warehouse),
               "--json", *args, env=env)


def checks(done):
    return {row["check"]: row for row in done.json["checks"]}


def test_status_lists_the_closed_month_with_its_state_and_hash(warehouse):
    done = status(warehouse)
    assert done.code == 0, done.stderr
    assert MONTH in done.stdout and "closed" in done.stdout
    assert "forecast runs: none" in done.stdout


def test_status_json_carries_the_months_and_an_empty_forecast_list(warehouse):
    done = status(warehouse, "--json")
    assert done.code == 0, done.stderr
    jsonschema.Draft202012Validator(ENVELOPE_SCHEMA).validate(done.json)
    (month,) = [m for m in done.json["months"] if m["month"] == MONTH]
    with db.connect(warehouse, read_only=True) as con:
        (expected,) = con.execute("SELECT result_hash FROM state.close_state WHERE month = ?", [MONTH]).fetchone()
    assert month["status"] == "closed" and month["result_hash"] == expected == "0" * 64
    assert done.json["forecast_runs"] == []


def test_status_lists_the_forecast_runs_by_as_of(warehouse):
    with db.connect(warehouse) as con:
        con.execute("CREATE TABLE forecast.runs (as_of DATE, \"case\" VARCHAR)")
        con.execute("INSERT INTO forecast.runs VALUES ('2026-03-31', 'base'), ('2026-02-28', 'upside'),"
                    " ('2026-02-28', 'base')")
    runs = status(warehouse, "--json").json["forecast_runs"]
    assert [(r["as_of"], r["case"]) for r in runs] == [("2026-02-28", "base"), ("2026-02-28", "upside"),
                                                       ("2026-03-31", "base")]


def test_status_without_a_warehouse_lists_nothing(tmp_path):
    done = status(tmp_path / "none.duckdb", "--json")
    assert done.code == 0, done.stderr
    assert done.json["months"] == [] and done.json["forecast_runs"] == []


def test_doctor_on_a_closed_warehouse_is_healthy(warehouse):
    done = doctor(warehouse)
    assert done.code == 0, done.stdout
    jsonschema.Draft202012Validator(ENVELOPE_SCHEMA).validate(done.json)
    assert {r["status"] for r in done.json["checks"]} == {"ok"}
    assert {"python", "versions", "books", "signoff", "warehouse", "stale reviewed pairs", "lock",
            "truth"} <= set(checks(done))


def test_doctor_without_a_signoff_warns(warehouse, tmp_path):
    done = doctor(warehouse, env={"C2C_SIGNOFF_DIR": str(tmp_path / "no-signoff")})
    assert done.code == 1, done.stdout
    assert done.json["status"] == "warning"
    assert checks(done)["signoff"]["status"] == "warn"


def test_doctor_on_a_corrupt_warehouse_fails(tmp_path):
    corrupt = tmp_path / "corrupt.duckdb"
    corrupt.write_bytes(b"this is not a duckdb file" * 100)
    done = doctor(corrupt)
    assert done.code == 2, done.stdout
    assert done.json["status"] == "failed"
    assert checks(done)["warehouse"]["status"] == "fail"


def test_doctor_without_a_warehouse_warns(tmp_path):
    done = doctor(tmp_path / "none.duckdb")
    assert done.code == 1, done.stdout
    assert checks(done)["warehouse"]["status"] == "warn"


def test_doctor_warns_about_a_reviewed_pair_that_no_longer_exists(warehouse, tmp_path):
    config_dir, signoff_dir = signed_config(tmp_path)
    matches = config_dir / "customer_matches.yaml"
    matches.write_text(matches.read_text() + "  - a: acct_9999\n    b: bill_9999\n    decision: same\n"
                                             "    reason: a customer that was removed\n")
    signoff.write_for(config_dir, signoff_dir, "Test signer (synthetic)", "finance_lead", "2026-01-01")
    done = doctor(warehouse, "--config", str(config_dir), env={"C2C_SIGNOFF_DIR": str(signoff_dir)})
    assert done.code == 1, done.stdout
    stale = checks(done)["stale reviewed pairs"]
    assert stale["status"] == "warn" and "acct_9999 / bill_9999" in stale["detail"]
    assert checks(done)["signoff"]["status"] == "ok"


def test_doctor_links_is_not_available(warehouse):
    done = doctor(warehouse, "--links")
    assert done.code == 2
    assert "not available" in done.json["message"]
