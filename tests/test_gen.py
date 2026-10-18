"""The assembled generator: byte-identical runs, the manifest, the `small` skeleton and the full budget."""

from __future__ import annotations

import csv
import json
import time

import pytest

from c2c import gen
from c2c.gen import BOOKS, TRUTH, manifest
from c2c.gen.truth import PLANTED

from conftest import c2c

SEED = 1


def read(path):
    with path.open(encoding="utf-8", newline="") as fh:
        return list(csv.DictReader(fh))


def rel(preset, *parts):
    """A path in the manifest's spelling: `books/` and `truth/` are flat for `mini`, nested otherwise."""
    head, *rest = parts
    return "/".join([head, *([] if preset in manifest.FLAT else [preset]), *rest])


def tree(root):
    return {p.relative_to(root).as_posix(): p.read_bytes() for p in sorted(root.rglob("*")) if p.is_file()}


@pytest.fixture(scope="module", params=["small", "mini"])
def written(request, tmp_path_factory):
    out = tmp_path_factory.mktemp(request.param)
    gen.generate(request.param, SEED, out)
    return request.param, out


@pytest.mark.parametrize("preset", ["small", "mini"])
def test_two_generations_are_byte_identical(preset, tmp_path):
    gen.generate(preset, SEED, tmp_path / "a")
    gen.generate(preset, SEED, tmp_path / "b")
    first, second = tree(tmp_path / "a"), tree(tmp_path / "b")
    assert first == second
    assert any(name.startswith("books/") for name in first) and any(name.startswith("truth/") for name in first)


def test_a_preset_and_the_full_books_cannot_share_a_flat_directory(tmp_path):
    gen.generate("mini", SEED, tmp_path)
    assert manifest.presets_under(tmp_path) == ["mini"]
    with pytest.raises(FileNotFoundError):
        gen.verify(tmp_path, "full")


def test_a_different_seed_gives_different_books(tmp_path):
    gen.generate("mini", 1, tmp_path / "a")
    gen.generate("mini", 2, tmp_path / "b")
    assert tree(tmp_path / "a") != tree(tmp_path / "b")


def test_the_manifest_verifies_and_catches_drift(written, tmp_path):
    preset, out = written
    assert gen.verify(out, preset) == {preset: []}
    header = manifest.read(manifest.manifest_path(out, preset))
    assert (header.preset, header.seed) == (preset, SEED)
    own_manifest = manifest.manifest_path(out, preset).relative_to(out).as_posix()
    assert set(header.files) == set(tree(out)) - {own_manifest}

    copy = tmp_path / "copy"
    gen.generate(preset, SEED, copy)
    books_dir, truth_dir = manifest.preset_dirs(copy, preset)
    target = books_dir / "invoices.csv"
    target.write_bytes(target.read_bytes() + b"x\n")
    (truth_dir / "stray.csv").write_text("a\n", encoding="utf-8")
    assert gen.verify(copy, preset)[preset] == [rel(preset, "books", "invoices.csv"), rel(preset, "truth", "stray.csv")]


def test_verify_without_a_manifest_is_an_error(tmp_path):
    with pytest.raises(FileNotFoundError):
        gen.verify(tmp_path)


def test_files_are_utf8_lf_sorted_and_known(written):
    preset, out = written
    books, truth_dir = manifest.preset_dirs(out, preset)
    assert {p.name for p in books.iterdir()} == {f"{stem}.csv" for stem in BOOKS} | {"window.json"}
    assert {p.name for p in truth_dir.iterdir()} == {f"{stem}.csv" for stem in TRUTH} | {PLANTED}
    for stem, (columns, key) in BOOKS.items():
        raw = (books / f"{stem}.csv").read_bytes()
        assert b"\r" not in raw and raw.endswith(b"\n")
        raw.decode("utf-8")
        rows = read(books / f"{stem}.csv")
        assert list(rows[0]) == list(columns) and "known_on" in columns, stem
        assert all(row["known_on"] for row in rows), stem
        numeric = {"version", "line_no"}
        keys = [tuple(int(r[k]) if k in numeric else r[k] for k in key) for r in rows]
        assert keys == sorted(keys), stem


def test_the_ledger_balances_and_recognises_revenue(written):
    preset, out = written
    books_dir, _ = manifest.preset_dirs(out, preset)
    rows = read(books_dir / "gl.csv")
    by_entry = {}
    for row in rows:
        by_entry[row["entry_id"]] = by_entry.get(row["entry_id"], 0) + int(row["amount_cents"])
    assert set(by_entry.values()) == {0}
    assert any(row["gl_account"] == "4000" for row in rows)
    window = json.loads((books_dir / "window.json").read_text(encoding="utf-8"))
    assert {row["gl_period"] for row in rows} <= {m for m in manifest_months(window)} | {"2025-12"}


def manifest_months(window):
    first, last = window["first_month"], window["last_month"]
    month, out = first, []
    while month <= last:
        out.append(month)
        year, mon = map(int, month.split("-"))
        month = f"{year + mon // 12:04d}-{mon % 12 + 1:02d}"
    return out


def test_small_keeps_its_customers_and_months(tmp_path):
    result = c2c("gen", "--fixture", "small", "--out", str(tmp_path))
    assert result.code == 0, result.stderr
    books = tmp_path / "books" / "small"
    assert [r["crm_account_id"] for r in read(books / "crm_accounts.csv")] == ["acct_0001", "acct_0002", "acct_0003"]
    assert json.loads((books / "window.json").read_text(encoding="utf-8")) == {
        "first_month": "2026-01", "last_month": "2026-03"}
    assert c2c("gen", "--verify", "--fixture", "small", "--out", str(tmp_path)).code == 0


def test_small_still_closes_with_the_receivable_tied(tmp_path):
    assert c2c("gen", "--fixture", "small", "--out", str(tmp_path)).code == 0
    result = c2c("close", "--month", "2026-03", "--fixture", "small", "--json",
                 "--books", str(tmp_path / "books" / "small"), "--warehouse", str(tmp_path / "w.duckdb"))
    assert result.code == 0, result.stderr
    controls = {c["id"]: c["status"] for c in result.json["controls"]}
    assert controls["CTL-TIE-AR"] == "pass"


def test_gen_verify_fails_on_drift_through_the_cli(tmp_path):
    assert c2c("gen", "--fixture", "mini", "--out", str(tmp_path)).code == 0
    (tmp_path / "books" / "gl.csv").write_text("changed\n", encoding="utf-8")
    result = c2c("gen", "--verify", "--fixture", "mini", "--out", str(tmp_path), "--json")
    assert result.code == 3
    assert result.json["failed_checks"][0]["drifted_files"] == ["books/gl.csv"]


def test_mini_defaults_to_the_committed_fixtures():
    result = c2c("gen", "--verify", "--fixture", "mini", "--json")
    assert result.code == 0, result.stderr
    assert result.json["preset"] == "mini"


@pytest.mark.slow
def test_full_generation_fits_the_budget(tmp_path):
    started = time.monotonic()
    built = gen.generate("full", SEED, tmp_path)
    assert time.monotonic() - started <= 30
    assert gen.verify(tmp_path, "full") == {"full": []}
    assert {p.name for p in (tmp_path / "books").iterdir()} >= {"invoices.csv", "gl.csv", "window.json"}
    assert len(built.files) == len(BOOKS) + 1 + len(TRUTH) + 1
    assert len(read(tmp_path / "books" / "crm_accounts.csv")) == 50
