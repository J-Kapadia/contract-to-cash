"""Controls: named checks written as SQL files that return their violating rows.

Each `controls/CTL-*.sql` starts with `-- remediation: <template>` and `-- stage: <stage>`. The
query returns one row per violation; any row fails the control. `$month` in the query is bound
to the month being closed. The template's `{column}` placeholders are columns of the returned
rows and are filled per row; a placeholder that is not a column is a defect in the control and
fails before any result is trusted. A control may also return `_row_hash` (the hashes of the
violating source rows) and `number_ids` (the published numbers they touch); both are lists.
"""

from __future__ import annotations

import json
import re
import uuid
from dataclasses import dataclass, field
from pathlib import Path

import duckdb

from .errors import ControlFailure, InternalError

CONTROLS_DIR = Path(__file__).resolve().parents[2] / "controls"
PLACEHOLDER = re.compile(r"\{(\w+)\}")
HEADER = re.compile(r"--\s*(remediation|stage):\s*(.*?)\s*$")


@dataclass
class ControlResult:
    id: str
    status: str  # "pass" or "fail"
    violating_rows: int
    detail: dict = field(default_factory=lambda: {"row_hashes": [], "number_ids": []})
    remediation: list[str] = field(default_factory=list)

    @property
    def failed(self) -> bool:
        return self.status == "fail"

    def summary(self) -> dict:
        """The entry of the envelope's `controls` list."""
        return {"id": self.id, "status": self.status, "violating_rows": self.violating_rows}

    def failure(self) -> dict:
        """The entry of the envelope's `failed_controls` list."""
        return {"id": self.id, "violating_rows": self.violating_rows, "detail": self.detail,
                "remediation": self.remediation}


def _header(path: Path) -> tuple[str, str]:
    """The remediation template and the stage from a control file's leading comment lines."""
    found: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.startswith("--"):
            break
        match = HEADER.fullmatch(line.strip())
        if match:
            found.setdefault(match.group(1), match.group(2))
    for key in ("remediation", "stage"):
        if not found.get(key):
            raise InternalError(f"{path.name} has no `-- {key}:` line at the top; add one.")
    return found["remediation"], found["stage"]


def _text(value: object) -> str:
    if isinstance(value, (list, tuple)):
        return ", ".join(str(item) for item in value)
    return "" if value is None else str(value)


def _fill(template: str, columns: list[str], row: tuple) -> str:
    values = dict(zip(columns, row))
    return PLACEHOLDER.sub(lambda m: _text(values[m.group(1)]), template)


def _listed(rows: list[tuple], columns: list[str], column: str) -> list[str]:
    if column not in columns:
        return []
    index = columns.index(column)
    return sorted({str(item) for row in rows for item in (row[index] or [])})


def _run_one(con: duckdb.DuckDBPyConnection, path: Path, month: str) -> ControlResult:
    template, _ = _header(path)
    sql = path.read_text(encoding="utf-8")
    cursor = con.execute(sql, {"month": month} if "$month" in sql else {})
    columns = [column[0] for column in cursor.description]
    unknown = sorted({name for name in PLACEHOLDER.findall(template) if name not in columns})
    if unknown:
        raise InternalError(f"{path.name}: the remediation names {', '.join('{' + n + '}' for n in unknown)}, "
                            f"which {'is' if len(unknown) == 1 else 'are'} not returned by the query "
                            f"(columns: {', '.join(columns)}); fix the template or the query.")
    rows = cursor.fetchall()
    detail = {"row_hashes": _listed(rows, columns, "_row_hash"), "number_ids": _listed(rows, columns, "number_ids")}
    return ControlResult(id=path.stem, status="fail" if rows else "pass", violating_rows=len(rows), detail=detail,
                         remediation=[_fill(template, columns, row) for row in rows])


def _record(con: duckdb.DuckDBPyConnection, run_id: str, month: str, results: list[ControlResult]) -> None:
    con.execute("CREATE SCHEMA IF NOT EXISTS ctl")
    con.execute("CREATE TABLE IF NOT EXISTS ctl.control_results (run_id VARCHAR, month VARCHAR, control_id VARCHAR,"
                " status VARCHAR, violating_rows BIGINT, detail JSON)")
    con.executemany("INSERT INTO ctl.control_results VALUES (?, ?, ?, ?, ?, ?)",
                    [(run_id, month, r.id, r.status, r.violating_rows, json.dumps(r.detail, sort_keys=True))
                     for r in results])


def run(con: duckdb.DuckDBPyConnection, month: str, stage: str, run_id: str | None = None,
        directory: str | Path = CONTROLS_DIR) -> list[ControlResult]:
    """Run every control of `stage` for `month`, record the results in `ctl.control_results` and
    return them. Raises `ControlFailure` listing each failed control when any has a violating row."""
    run_id = run_id or uuid.uuid4().hex
    results = [_run_one(con, path, month) for path in sorted(Path(directory).glob("CTL-*.sql"))
               if _header(path)[1] == stage]
    _record(con, run_id, month, results)
    failed = [r for r in results if r.failed]
    if failed:
        raise ControlFailure(f"{month} stays open: {', '.join(r.id for r in failed)} failed.",
                             [r.failure() for r in failed])
    return results
