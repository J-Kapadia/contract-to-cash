"""Building a forecast over a copy of the closed `mini` warehouse.

`mini_closed_copy` is a fixture a test module imports; `forecast_built` runs one forecast build
over the copy it is given.
"""

from __future__ import annotations

import shutil
from datetime import date
from pathlib import Path

import pytest

from c2c import forecast, policy
from conftest import DEFAULT_CONFIG, MINI_BOOKS, MINI_WINDOW, c2c, shared_dir


@pytest.fixture(scope="session")
def mini_closed(tmp_path_factory) -> Path:
    """The `mini` books closed through their last month once for the whole run; read-only."""

    def build(root: Path) -> None:
        done = c2c("close", "--month", MINI_WINDOW[1], "--books", str(MINI_BOOKS),
                   "--warehouse", str(root / "w.duckdb"), "--json")
        assert done.code == 0, done.stderr

    return shared_dir(tmp_path_factory, "closed-mini", build) / "w.duckdb"


@pytest.fixture
def mini_closed_copy(mini_closed, tmp_path) -> Path:
    """A private copy of the closed `mini` warehouse, for a test that changes it."""
    copy = tmp_path / "w.duckdb"
    shutil.copyfile(mini_closed, copy)
    return copy


def forecast_built(warehouse: Path, as_of: date) -> Path:
    """Build the forecast as of `as_of` into `fc_build` of `warehouse` under the `mini` policy; returns it."""
    forecast.build(warehouse, as_of, policy.load(DEFAULT_CONFIG))
    return warehouse
