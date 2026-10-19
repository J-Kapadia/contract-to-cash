"""`explain` over every family of the number-id grammar on a closed `mini`: each family resolves, a
leaf lists the raw rows its digest was taken over, a composite its children down to `--depth`, and
each call stays under two seconds."""

from __future__ import annotations

import shutil
import time
from pathlib import Path

import pytest

from c2c import db, prov

from conftest import MINI_BOOKS, c2c, shared_dir

MONTH = "2026-03"
# The families a month's close writes records for, then the two the grammar also names.
FAMILIES = ["cust", "rev", "defrev", "ar", "cash", "tie", "aging", "dtp", "bridge", "rollup", "master", "ref", "cfg"]
BUDGET_SECONDS = 2.0


@pytest.fixture(scope="module")
def warehouse(tmp_path_factory) -> Path:
    def build(directory: Path) -> None:
        done = c2c("close", "--month", MONTH, "--fixture", "mini", "--books", str(MINI_BOOKS),
                   "--warehouse", str(directory / "w.duckdb"), "--json", env={"C2C_BOOKS": ""})
        assert done.code == 0, done.stderr

    return shared_dir(tmp_path_factory, "explain-mini", build) / "w.duckdb"


def run(warehouse: Path, *args: str):
    return c2c("explain", *args, "--books", str(MINI_BOOKS), "--warehouse", str(warehouse))


def query(warehouse: Path, sql: str, params=()):
    with db.connect(warehouse, read_only=True) as con:
        return con.execute(sql, params).fetchall()


def sample(warehouse: Path, family: str) -> str | None:
    """The first id of the family for the closed month, or for no month when the family has none."""
    rows = query(warehouse, "SELECT number_id FROM prov.records WHERE split_part(number_id, '/', 1) = ?"
                 " AND coalesce(month, ?) = ? ORDER BY number_id", [family, MONTH, MONTH])
    return rows[0][0] if rows else None


def stored(warehouse: Path, number_id: str) -> tuple[str, int]:
    return query(warehouse, "SELECT input_digest, input_row_count FROM prov.records WHERE number_id = ?",
                 [number_id])[0]


def hashes(warehouse: Path, number_id: str) -> set[str]:
    (source_query,) = query(warehouse, "SELECT source_query FROM prov.records WHERE number_id = ?", [number_id])[0]
    return {h for (h,) in query(warehouse, f"SELECT _row_hash FROM ({source_query})")}


def row_hashes(source) -> set[str]:
    return {row[source["columns"].index("_row_hash")] for row in source["rows"]}


@pytest.mark.parametrize("family", FAMILIES)
def test_one_id_of_each_family_resolves_within_the_budget(warehouse, family):
    number_id = sample(warehouse, family)
    if number_id is None:
        pytest.skip(f"a close writes no {family}/ records yet")
    started = time.monotonic()
    done = run(warehouse, number_id, "--json")
    elapsed = time.monotonic() - started
    assert done.code == 0, done.stderr
    assert elapsed < BUDGET_SECONDS, f"{number_id} took {elapsed:.2f} s"
    chain = done.json["explanation"]
    digest, count = stored(warehouse, number_id)
    assert chain["record"]["number_id"] == number_id and chain["digest_matches"] is True
    assert (chain["derived_digest"], chain["derived_row_count"]) == (digest, count)
    if chain["record"]["source_query"]:  # a leaf lists exactly the rows its digest was taken over
        assert chain["children"] == []
        assert set().union(*(row_hashes(s) for s in chain["sources"])) == hashes(warehouse, number_id)
        assert all("_row_hash" in s["columns"] for s in chain["sources"])
    else:  # a composite lists its children, each with its own record
        assert chain["sources"] == [] and len(chain["children"]) == count > 0
        pairs = [(c["record"]["number_id"], stored(warehouse, c["record"]["number_id"])[0]) for c in chain["children"]]
        assert prov.pair_digest(pairs) == digest
        assert all(c["digest_matches"] and c["children"] == [] and c["sources"] == [] for c in chain["children"])


def test_a_leaf_of_config_and_of_reference_data_prints_its_row(warehouse):
    config = run(warehouse, sample(warehouse, "cfg"), "--json").json["explanation"]
    (policy,) = config["sources"]
    assert policy["table"] == "ref_policy" and len(policy["rows"]) == 1
    assert config["record"]["number_id"].removeprefix("cfg/") == policy["rows"][0][policy["columns"].index("key")]
    reference = run(warehouse, "ref/business_days/2024-10-01", "--json").json["explanation"]
    (days,) = reference["sources"]
    assert days["table"] == "ref_business_days" and len(days["rows"]) == 1
    assert "2024-10-01" in days["rows"][0]


def test_a_composite_walks_to_its_leaves_at_depth_two(warehouse):
    number_id = f"bridge/{MONTH}/revenue"
    tree = run(warehouse, number_id, "--depth", "2", "--json").json["explanation"]
    assert tree["digest_matches"] is True and tree["children"]
    for child in tree["children"]:
        assert child["digest_matches"] is True and child["sources"] and child["children"] == []
        assert set().union(*(row_hashes(s) for s in child["sources"])) == hashes(warehouse, child["record"]["number_id"])


def test_depth_zero_prints_the_record_of_a_composite_only(warehouse):
    tree = run(warehouse, f"bridge/{MONTH}/revenue", "--depth", "0", "--json").json["explanation"]
    assert tree["children"] == [] and tree["sources"] == [] and tree["digest_matches"] is True
    text = run(warehouse, f"bridge/{MONTH}/revenue", "--depth", "0")
    assert text.code == 0 and text.stdout.count("\n  digest:") == 1 and "re-derived matches" in text.stdout


def test_the_text_tree_indents_each_level_under_its_parent(warehouse):
    done = run(warehouse, f"aging/{MONTH}/current", "--depth", "2")
    assert done.code == 0, done.stderr
    lines = done.stdout.splitlines()
    assert lines[0] == f"aging/{MONTH}/current"
    children = [line for line in lines if line.startswith(f"    aging/{MONTH}/") and line.endswith("/current")]
    assert children and all(not line.startswith("     ") for line in children)
    assert any(line.startswith("      invoices (") for line in lines)


def test_an_unknown_family_exits_2_with_at_most_three_nearest(warehouse):
    done = run(warehouse, f"arr/{MONTH}/acct_0001/closing")
    assert done.code == 2
    suggested = [w for w in done.stderr.replace(",", " ").replace("?", " ").split() if w.startswith("ar/")]
    assert 1 <= len(suggested) <= 3
    assert run(warehouse, f"nosuch/{MONTH}/x").code == 2


def test_a_tampered_child_exits_6_naming_the_child(warehouse, tmp_path):
    copy = tmp_path / "w.duckdb"
    shutil.copy(warehouse, copy)
    child = sample(warehouse, "rev")
    with db.connect(copy) as con:
        con.execute("UPDATE prov.records SET input_digest = repeat('0', 64) WHERE number_id = ?", [child])
    done = c2c("explain", f"bridge/{MONTH}/revenue", "--depth", "2", "--warehouse", str(copy), "--json")
    assert done.code == 6
    assert child in done.json["message"]
