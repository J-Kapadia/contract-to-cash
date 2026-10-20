"""The README names every command, control and injectable error, carries no stale or private wording."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from c2c import inject
from c2c.gen.plan import INJECT_ERRORS

from conftest import ROOT, c2c

README = (ROOT / "README.md").read_text(encoding="utf-8")
STALE = ("Status", "in flight", "not built", "planned", "TODO")


def _patterns(path: Path) -> list[str]:
    return [line for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.lstrip().startswith("#")]


def _commands() -> list[str]:
    help_text = c2c("--help").stdout
    block = help_text.split("Commands:", 1)[1]
    return re.findall(r"^  ([\w-]+)\s", block, flags=re.MULTILINE)


def test_the_command_list_is_parsed() -> None:
    assert {"close", "explain", "gen", "inject", "memo"} <= set(_commands())


@pytest.mark.parametrize("name", _commands())
def test_readme_names_every_command(name: str) -> None:
    assert f"`c2c {name}`" in README


@pytest.mark.parametrize("control", sorted(path.stem for path in (ROOT / "controls").glob("CTL-*.sql")))
def test_readme_names_every_control(control: str) -> None:
    assert f"`{control}`" in README


def test_readme_lists_the_controls_in_file_name_order() -> None:
    ids = sorted(path.stem for path in (ROOT / "controls").glob("CTL-*.sql"))
    section = README.split("## Controls", 1)[1].split("\n## ", 1)[0]
    listed = re.findall(r"^- `(CTL-[A-Z-]+)`", section, flags=re.MULTILINE)
    assert listed == ids


@pytest.mark.parametrize("error", INJECT_ERRORS)
def test_readme_names_every_injectable_error(error: str) -> None:
    assert f"`{error}`" in README


def test_readme_names_the_errors_that_inject_accepts() -> None:
    refused = c2c("inject", "--error", "no_such_error", "--out", "unused").stderr
    for error in re.findall(r"\b[a-z]+(?:_[a-z]+)+\b", refused.split("answer-key errors:", 1)[1].split(";", 1)[0]):
        assert f"`{error}`" in README
    assert set(INJECT_ERRORS) == set(inject.APPLY)


@pytest.mark.parametrize("word", STALE)
def test_readme_has_no_stale_wording(word: str) -> None:
    assert word not in README


@pytest.mark.parametrize("pattern", _patterns(ROOT / "scripts" / "private-terms.txt"))
def test_readme_carries_no_private_process_string(pattern: str) -> None:
    assert re.search(pattern, README) is None


def test_readme_keeps_the_standard_sentence() -> None:
    assert "ASC 606" in README and "IFRS 15" in README
