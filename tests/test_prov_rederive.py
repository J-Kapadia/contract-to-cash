"""Re-deriving digests over the `mini` books: every recorded number's digest follows the source rows it is built on."""

from __future__ import annotations

import json
import random
import shutil
from pathlib import Path

import pytest

from c2c import check, controls, db, prov, state
from c2c.errors import ProvenanceError
from conftest import MINI_BOOKS, MINI_TRUTH, MINI_WINDOW, c2c, shared_dir

MONTH = "2026-03"
SEED = 20261014
PER_MONTH = 20


def close(warehouse: Path, *extra: str):
    # the harness points C2C_BOOKS at a private root, which would win over the fixture's books
    return c2c("close", "--month", MONTH, "--fixture", "mini", "--books", str(MINI_BOOKS), "--truth", str(MINI_TRUTH),
               "--warehouse", str(warehouse), "--check", "--json", *extra)


@pytest.fixture(scope="module")
def checked(tmp_path_factory):
    """`mini` closed through 2026-03 with `--check`: its warehouse and the first run's envelope. Read-only:
    a test that changes the warehouse takes a copy."""

    def build(directory: Path) -> None:
        done = close(directory / "w.duckdb")
        assert done.code == 0, done.stderr
        (directory / "first.json").write_text(done.stdout, encoding="utf-8")

    root = shared_dir(tmp_path_factory, "prov-rederive-mini", build)
    return root / "w.duckdb", json.loads((root / "first.json").read_text(encoding="utf-8"))


@pytest.fixture
def warehouse_copy(checked, tmp_path) -> Path:
    shutil.copy(checked[0], tmp_path / "w.duckdb")
    return tmp_path / "w.duckdb"


def all_digests(con) -> dict[str, tuple[str, str]]:
    """`(stored, recomputed)` for every recorded number."""
    memo: dict = {}
    return {n: prov.rederive(con, n, memo) for (n,) in con.execute("SELECT number_id FROM prov.records ORDER BY 1").fetchall()}


def test_close_check_reports_ctl_prov_passing(checked):
    controls_ = {c["id"]: c for c in checked[1]["controls"]}
    assert controls_["CTL-PROV"]["status"] == "pass" and controls_["CTL-PROV"]["violating_rows"] == 0


def test_a_plain_close_does_not_list_ctl_prov(tmp_path):
    done = c2c("close", "--month", "2024-10", "--fixture", "mini", "--books", str(MINI_BOOKS),
               "--warehouse", str(tmp_path / "w.duckdb"), "--json")
    assert done.code == 0, done.stderr
    assert "CTL-PROV" not in [c["id"] for c in done.json["controls"]]


def test_sampled_ids_of_every_month_re_derive(checked):
    rng = random.Random(SEED)
    memo: dict = {}
    with db.connect(checked[0], read_only=True) as con:
        for month in state.months(*MINI_WINDOW):
            ids = [n for (n,) in con.execute("SELECT number_id FROM prov.records WHERE month = ? ORDER BY 1",
                                             [month]).fetchall()]
            assert ids, month
            for number_id in rng.sample(ids, min(PER_MONTH, len(ids))):
                stored, recomputed = prov.rederive(con, number_id, memo)
                assert stored == recomputed, number_id


def selected(con, number_id: str) -> set[str]:
    """The row hashes a leaf's source query selects now."""
    (query,) = con.execute("SELECT source_query FROM prov.records WHERE number_id = ?", [number_id]).fetchone()
    return {r[0] for r in con.execute(query).fetchall()}


def family_graph(con) -> tuple[dict[str, set[str]], dict[str, list[str]]]:
    """The leaves' source row hashes and the composites' children, as the family files declare them now."""
    sources = [n for (n,) in con.execute("SELECT number_id FROM prov.records WHERE source_query IS NOT NULL").fetchall()]
    leaves = {n: selected(con, n) for n in sources}
    children: dict[str, list[str]] = {}
    for month in state.months(*MINI_WINDOW):
        for path in prov.month_families():
            for row in prov.family_rows(con, path, month):
                if row["kind"] == prov.COMPOSITE:
                    children[row["number_id"]] = sorted(set(row["children"]))
    return leaves, children


def built_on(found: set[str], children: dict[str, list[str]]) -> set[str]:
    """`found` and every composite above it, through composites of any family."""
    found = set(found)
    while True:
        more = {n for n, kids in children.items() if n not in found and found.intersection(kids)}
        if not more:
            return found
        found |= more


def test_editing_one_source_row_changes_exactly_the_digests_built_on_it(warehouse_copy):
    with db.connect(warehouse_copy) as con:
        leaves, children = family_graph(con)
        used = set().union(*leaves.values())
        (row_hash,) = con.execute("SELECT min(_row_hash) FROM raw.invoices WHERE _row_hash IN (SELECT unnest(?))",
                                  [sorted(used)]).fetchone()
        selecting = sorted(n for n, hashes in leaves.items() if row_hash in hashes)
        con.execute("UPDATE raw.invoices SET _row_hash = 'edited-' || _row_hash WHERE _row_hash = ?", [row_hash])
        # A leaf that reads the row through a model built earlier still selects the old hash: only the
        # leaves that read the edited table change.
        changed = {n for n in selecting if row_hash not in selected(con, n)}
        expected = built_on(changed, children)
        assert changed and any(n in children for n in expected)
        assert len(expected) < len(leaves) + len(children)

        with pytest.raises(ProvenanceError) as caught:
            check.close_check(con, MONTH, MINI_TRUTH)
    error = caught.value
    assert error.exit == 6
    [failed] = error.failed_controls
    assert failed["id"] == "CTL-PROV"
    assert set(failed["detail"]["number_ids"]) == expected
    first = min(expected)
    assert failed["detail"]["number_ids"][0] == first
    assert failed["remediation"][0] == (
        f"Number {first} does not re-derive: delete the warehouse and re-run c2c close; "
        "if it persists, a table was edited outside c2c.")
    assert first in error.message


def test_close_check_exits_6_naming_a_number_whose_stored_digest_was_edited(warehouse_copy):
    number = f"ar/{MONTH}/acct_0001/closing"
    with db.connect(warehouse_copy) as con:
        con.execute("UPDATE prov.records SET input_digest = 'edited' WHERE number_id = ?", [number])
    done = close(warehouse_copy)
    assert done.code == 6, done.stderr
    [failed] = done.json["failed_controls"]
    assert failed["id"] == "CTL-PROV"
    assert failed["detail"]["number_ids"] == [number]
    assert number in done.json["message"]


def test_a_published_number_without_a_record_is_unrecorded(warehouse_copy):
    number = f"ar/{MONTH}/acct_0001/closing"
    with db.connect(warehouse_copy) as con:
        assert controls.unrecorded(con, MONTH) == []
        con.execute("DELETE FROM prov.records WHERE number_id = ?", [number])
        assert controls.unrecorded(con, MONTH) == [number]
