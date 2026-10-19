"""`c2c inject` drift errors: one module per error in a directory, planted from the books and a month."""

from __future__ import annotations

import csv
import hashlib
import textwrap
from pathlib import Path

import pytest
from click.testing import CliRunner

from c2c import cli, drift
from c2c.errors import C2CError
from c2c.gen.manifest import read as read_manifest
from c2c.gen.plan import INJECT_ERRORS
from c2c.inject import MANIFEST

from conftest import MINI_BOOKS, c2c

STUB = textwrap.dedent('''
    def apply(books, month, rng):
        gl = books["gl"]
        position = gl.where(gl_period=month)[rng.randrange(len(gl.where(gl_period=month)))]
        gl.set(position, amount_cents=int(gl.rows[position][0]["amount_cents"]) + 1)
        return books.save()
''')


@pytest.fixture
def stubs(tmp_path) -> Path:
    directory = tmp_path / "drift_modules"
    directory.mkdir()
    (directory / "zeta_gap.py").write_text(STUB, encoding="utf-8")
    (directory / "alpha_gap.py").write_text(STUB, encoding="utf-8")
    (directory / "__init__.py").write_text("", encoding="utf-8")
    return directory


def tree(root: Path) -> dict[str, bytes]:
    return {p.name: p.read_bytes() for p in sorted(root.iterdir()) if p.is_file()}


def test_errors_lists_module_stems_sorted(stubs):
    assert drift.errors(stubs) == ("alpha_gap", "zeta_gap")


def test_errors_of_an_empty_directory(tmp_path):
    assert drift.errors(tmp_path) == ()


def test_inject_drift_changes_one_row_and_is_repeatable(stubs, tmp_path):
    first = drift.inject_drift(MINI_BOOKS, tmp_path / "a", "alpha_gap", "2026-01", 7, stubs)
    again = drift.inject_drift(MINI_BOOKS, tmp_path / "b", "alpha_gap", "2026-01", 7, stubs)

    assert (first.error, first.month) == ("alpha_gap", "2026-01")
    assert len(first.changed) == 1 and first.changed[0]["file"] == "gl.csv" and first.changed[0]["change"] == "edited"
    assert tree(tmp_path / "a") == tree(tmp_path / "b")

    clean = {p.name: p.read_bytes() for p in MINI_BOOKS.iterdir() if p.is_file() and p.name != MANIFEST}
    copy = tree(tmp_path / "a")
    assert copy.keys() - {MANIFEST} == clean.keys()
    assert [name for name in clean if copy[name] != clean[name]] == ["gl.csv"]
    differing = [(a, b) for a, b in zip(clean["gl.csv"].splitlines(), copy["gl.csv"].splitlines()) if a != b]
    assert len(differing) == 1
    assert next(csv.reader([differing[0][1].decode()]))[0] == first.changed[0]["key"]["entry_id"]

    manifest = read_manifest(tmp_path / "a" / MANIFEST)
    assert set(manifest.files) == set(clean)
    assert manifest.files["gl.csv"] == hashlib.sha256(copy["gl.csv"]).hexdigest()
    assert manifest.preset == read_manifest(MINI_BOOKS.parent / MANIFEST).preset
    assert again.manifest == tmp_path / "b" / MANIFEST


def test_inject_drift_refuses_a_non_empty_out(stubs, tmp_path):
    (tmp_path / "out").mkdir()
    (tmp_path / "out" / "x").write_text("x", encoding="utf-8")
    with pytest.raises(C2CError) as caught:
        drift.inject_drift(MINI_BOOKS, tmp_path / "out", "alpha_gap", "2026-01", 1, stubs)
    assert caught.value.exit == 2


def test_inject_drift_needs_books_with_a_window(stubs, tmp_path):
    (tmp_path / "empty").mkdir()
    with pytest.raises(C2CError) as caught:
        drift.inject_drift(tmp_path / "empty", tmp_path / "out", "alpha_gap", "2026-01", 1, stubs)
    assert caught.value.exit == 4


def test_cli_unknown_error_names_both_groups(tmp_path):
    result = c2c("inject", "--error", "no_such_error", "--books", str(MINI_BOOKS), "--out", str(tmp_path / "o"))
    assert result.code == 2
    assert "answer-key errors" in result.stderr and "drift errors" in result.stderr
    assert INJECT_ERRORS[0] in result.stderr


def test_cli_month_is_a_usage_error_for_an_answer_key_error(tmp_path):
    result = c2c("inject", "--error", "rounding_slip", "--month", "2026-01", "--books", str(MINI_BOOKS),
                 "--out", str(tmp_path / "o"))
    assert result.code == 2
    assert "--month" in result.stderr
    assert not (tmp_path / "o").exists()


def test_cli_month_outside_the_window_is_a_usage_error(monkeypatch, tmp_path):
    monkeypatch.setattr(drift, "errors", lambda directory=drift.HERE: ("alpha_gap",))
    result = CliRunner().invoke(cli.main, ["inject", "--error", "alpha_gap", "--month", "2030-01",
                                           "--books", str(MINI_BOOKS), "--out", str(tmp_path / "o")])
    assert result.exit_code == 2
    assert "outside the books' window" in result.output
    assert not (tmp_path / "o").exists()
