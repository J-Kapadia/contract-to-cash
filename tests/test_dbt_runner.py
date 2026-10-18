"""A dbt failure is an internal error: exit 70 with dbt's last fifteen lines, and dbt starts once per close."""

from __future__ import annotations

import shutil

from c2c import dbt_runner

MONTH = "2026-01"
DEFECT_LINES = 40  # the scratch model raises an error of this many lines, more than the tail kept


def project_with_failing_model(tmp_path):
    """A copy of the dbt project with one extra model whose compilation fails."""
    project = tmp_path / "dbt"
    shutil.copytree(dbt_runner.DBT_DIR, project, ignore=shutil.ignore_patterns("target", "logs", "dbt_packages"))
    detail = "\\n".join(f"defect line {n:02d}" for n in range(1, DEFECT_LINES + 1))
    (project / "models" / "marts" / "zz_scratch_failure.sql").write_text(
        "{{ exceptions.raise_compiler_error('" + detail + "') }}\nselect 1 as one\n", encoding="utf-8")
    return project


def test_a_failing_model_exits_70_with_the_last_fifteen_dbt_lines(mini_books_copy, workspace, tmp_path):
    trace = tmp_path / "dbt-trace.txt"
    done = workspace.run("close", "--month", MONTH, "--json",
                         env={dbt_runner.DBT_DIR_VAR: str(project_with_failing_model(tmp_path)),
                              dbt_runner.TRACE_VAR: str(trace)})

    assert done.code == 70, done.stderr
    assert done.json["status"] == "failed" and done.json["exit"] == 70
    head, *tail = done.json["message"].splitlines()
    assert head == "dbt build failed:"
    assert len(tail) == dbt_runner.TAIL_LINES == 15
    assert tail[-1].strip().endswith(f"defect line {DEFECT_LINES:02d}")
    assert len(trace.read_text(encoding="utf-8").splitlines()) == 1


def test_a_clean_close_starts_dbt_once(mini_books_copy, workspace, tmp_path):
    trace = tmp_path / "dbt-trace.txt"
    done = workspace.run("close", "--month", MONTH, "--json", env={dbt_runner.TRACE_VAR: str(trace)})

    assert done.code == 0, done.stderr
    [call] = trace.read_text(encoding="utf-8").splitlines()
    assert call.startswith("build ") and "--exclude tag:forecast" in call
