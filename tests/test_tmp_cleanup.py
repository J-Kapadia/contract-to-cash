"""A pytest process removes the scratch directories it made under the temporary directory."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

from conftest import ROOT

# One test that runs `c2c` (private default root, signed configuration) and one that renders the
# warehouse macros through dbt (macro project and scratch database).
QUICK_TESTS = [
    "tests/test_duckdb_limits.py::test_c2c_helper_isolates_the_run_from_the_environment",
    "tests/test_macros.py::test_ratable_cents_by_day_across_a_leap_february",
]


def test_a_finished_session_leaves_no_scratch_directory_behind(tmp_path):
    tmpdir = tmp_path / "tmp"
    tmpdir.mkdir()
    env = {k: v for k, v in os.environ.items() if not k.startswith("PYTEST_")}
    env["TMPDIR"] = str(tmpdir)
    proc = subprocess.run(
        ["uv", "run", "pytest", "-q", "-p", "no:cacheprovider", *QUICK_TESTS],
        cwd=ROOT, env=env, capture_output=True, text=True, check=False, timeout=540)
    assert proc.returncode == 0, proc.stdout[-2000:] + proc.stderr[-2000:]
    left = sorted(p.name for pattern in ("c2c-defaults-*", "c2c-macros-*", "c2c-macro-db-*", "c2c-signoff-*")
                  for p in Path(tmpdir).glob(pattern))
    assert left == []
