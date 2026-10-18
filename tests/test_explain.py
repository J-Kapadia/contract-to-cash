"""`c2c explain`: a number's provenance chain, down to the source rows."""

from __future__ import annotations

import json
import shutil
import time
from pathlib import Path

import jsonschema
import pytest

from c2c import db, explain, lock
from c2c.errors import LockHeld

from conftest import Closed, c2c

ENVELOPE_SCHEMA = json.loads((Path(__file__).resolve().parents[1] / "src/c2c/schemas/envelope.schema.json").read_text())
NUMBER_ID = "ar/2026-03/acct_0001/closing"


@pytest.fixture(scope="module")
def copy(closed, tmp_path_factory):
    """A private copy of the closed warehouse: another test closes the shared one again, which holds
    its lock exclusively."""
    warehouse = tmp_path_factory.mktemp("explain") / "w.duckdb"
    for _ in range(600):
        try:
            with lock.held(closed.warehouse, "shared"):  # wait out a close of the shared one
                shutil.copy(closed.warehouse, warehouse)
            break
        except LockHeld:
            time.sleep(0.1)
    else:
        pytest.fail("the closed warehouse stayed locked")
    return Closed(books=closed.books, warehouse=warehouse)


def run(where, *args: str):
    return c2c("explain", *args, "--books", str(where.books), "--warehouse", str(where.warehouse))


def test_explain_prints_the_value_the_step_and_the_source_rows(copy):
    done = run(copy, NUMBER_ID)
    assert done.code == 0, done.stderr
    assert NUMBER_ID in done.stdout and "fct_ar_rollforward" in done.stdout and "matches" in done.stdout
    assert "invoices (" in done.stdout and "payments (" in done.stdout
    with db.connect(copy.warehouse, read_only=True) as con:
        (cents,) = con.execute("SELECT value_cents FROM prov.records WHERE number_id = ?", [NUMBER_ID]).fetchone()
    assert f"value:   {cents} cents" in done.stdout


def test_the_json_envelope_validates_and_carries_the_chain(copy):
    done = run(copy, NUMBER_ID, "--json")
    assert done.code == 0, done.stderr
    jsonschema.Draft202012Validator(ENVELOPE_SCHEMA).validate(done.json)
    chain = done.json["explanation"]
    assert chain["digest_matches"] is True and chain["record"]["number_id"] == NUMBER_ID
    assert {s["table"] for s in chain["sources"]} >= {"invoices", "payments"}
    assert sum(len(s["rows"]) for s in chain["sources"]) == chain["record"]["input_row_count"]
    assert all("_row_hash" in s["columns"] for s in chain["sources"])


def test_depth_zero_stops_at_the_record(copy):
    done = run(copy, NUMBER_ID, "--depth", "0", "--json")
    assert done.code == 0, done.stderr
    assert done.json["explanation"]["sources"] == []
    assert done.json["explanation"]["digest_matches"] is True


def test_an_unknown_id_exits_2_and_suggests_the_nearest(copy):
    done = run(copy, "ar/2026-03/acct_0001/closng")
    assert done.code == 2
    assert NUMBER_ID in done.stderr
    assert len([w for w in done.stderr.replace(",", " ").split() if w.startswith("ar/")]) <= 3


def test_an_unknown_id_far_from_any_suggests_nothing(copy):
    with db.connect(copy.warehouse, read_only=True) as con:
        assert explain.nearest(con, "zzz") == []
    assert run(copy, "zzz").code == 2


def test_a_tampered_digest_exits_6(copy, tmp_path):
    warehouse = tmp_path / "w.duckdb"
    shutil.copy(copy.warehouse, warehouse)
    with db.connect(warehouse) as con:
        con.execute("UPDATE prov.records SET input_digest = repeat('0', 64) WHERE number_id = ?", [NUMBER_ID])
    done = c2c("explain", NUMBER_ID, "--warehouse", str(warehouse), "--json")
    assert done.code == 6
    assert done.json["status"] == "failed" and NUMBER_ID in done.json["message"]


def test_a_missing_warehouse_exits_4(tmp_path):
    assert c2c("explain", NUMBER_ID, "--warehouse", str(tmp_path / "none.duckdb")).code == 4
