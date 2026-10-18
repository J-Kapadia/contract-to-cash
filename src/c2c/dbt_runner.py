"""Running dbt as a subprocess against the warehouse.

dbt opens the DuckDB file itself, so the caller closes its own connection before `build` and
reopens afterwards.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from collections.abc import Mapping
from pathlib import Path

from .errors import InternalError

DBT_DIR = Path(__file__).resolve().parents[2] / "dbt"
DBT_DIR_VAR = "C2C_DBT_DIR"  # a copy of the dbt project to run instead of the one in the checkout
TRACE_VAR = "C2C_DBT_TRACE"  # a file that gets one line appended for every dbt subprocess started
TAIL_LINES = 15


def project_dir() -> Path:
    """The dbt project to run: `C2C_DBT_DIR` when set, else the checkout's `dbt/`."""
    return Path(os.environ.get(DBT_DIR_VAR) or DBT_DIR)


def scratch_dir(warehouse: str | Path) -> Path:
    """Where dbt writes its target and logs: beside the warehouse, never inside the project."""
    warehouse = Path(warehouse)
    return warehouse.with_name(warehouse.name + ".dbt")


def build(warehouse: str | Path, select: str | None = None, exclude: str | None = "tag:forecast",
          vars: Mapping[str, object] | None = None) -> None:
    """Run `dbt build` over the project against `warehouse`; models tagged `forecast` are left out
    unless `exclude` says otherwise. Raises `InternalError` with dbt's last lines on a failure."""
    warehouse = Path(warehouse).resolve()
    scratch = scratch_dir(warehouse)
    scratch.mkdir(parents=True, exist_ok=True)
    project = project_dir()
    args = ["build", "--project-dir", str(project), "--profiles-dir", str(project), "--no-use-colors",
            "--quiet", "--target-path", str(scratch / "target"), "--log-path", str(scratch / "logs")]
    if select:
        args += ["--select", select]
    if exclude:
        args += ["--exclude", exclude]
    if vars:
        args += ["--vars", json.dumps(dict(vars), sort_keys=True)]
    env = {**os.environ, "C2C_WAREHOUSE": str(warehouse), "DBT_SEND_ANONYMOUS_USAGE_STATS": "false"}
    trace = os.environ.get(TRACE_VAR)
    if trace:
        with open(trace, "a", encoding="utf-8") as fh:
            fh.write(" ".join(args) + "\n")
    proc = subprocess.run([sys.executable, "-c", "from dbt.cli.main import cli; cli()", *args],
                          env=env, capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        lines = (proc.stdout + proc.stderr).strip().splitlines()[-TAIL_LINES:]
        raise InternalError("dbt build failed:\n" + "\n".join(lines))
