"""The public workflows: what CI runs, and what the Pages deploy may touch."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

WORKFLOWS = Path(__file__).resolve().parent.parent / ".github" / "workflows"


def load(name: str) -> dict:
    return yaml.safe_load((WORKFLOWS / name).read_text())


def triggers(workflow: dict) -> dict:
    # YAML 1.1 reads the bare key `on` as the boolean True.
    return workflow.get("on", workflow.get(True))


def steps(workflow: dict):
    for job in workflow["jobs"].values():
        yield from job.get("steps", [])


def runs(workflow: dict) -> list[str]:
    return [step["run"] for step in steps(workflow) if "run" in step]


def test_pages_permissions_are_exactly_the_deploy_set():
    pages = load("pages.yml")
    assert pages["permissions"] == {"pages": "write", "id-token": "write", "contents": "read"}
    for job in pages["jobs"].values():
        assert "permissions" not in job


def test_pages_runs_on_version_tags_only():
    assert triggers(load("pages.yml")) == {"push": {"tags": ["v*"]}}


def test_pages_builds_the_site_then_uploads_and_deploys_it():
    pages = load("pages.yml")
    build = (
        "uv run c2c gen && uv run c2c rollforward --month 2026-09"
        " && uv run c2c memo --month 2026-09 --check --crawl --out site"
    )
    assert build in runs(pages)
    uses = [step["uses"] for step in steps(pages) if "uses" in step]
    assert any(u.startswith("actions/upload-pages-artifact@") for u in uses)
    assert any(u.startswith("actions/deploy-pages@") for u in uses)


def test_ci_runs_on_push_and_pull_request():
    assert set(triggers(load("ci.yml"))) == {"push", "pull_request"}


def test_ci_installs_from_the_lock_and_checks_it():
    ci = load("ci.yml")
    for name in ("fast", "slow"):
        commands = "\n".join(step.get("run", "") for step in ci["jobs"][name]["steps"])
        assert "uv sync --frozen" in commands
        assert "uv lock --check" in commands
        assert "pip install uv==0.13.0" in commands


def test_ci_fast_job_runs_the_fast_tier_under_a_timeout():
    fast = load("ci.yml")["jobs"]["fast"]
    assert "timeout 900 scripts/definition-of-done.sh --fast" in [s.get("run") for s in fast["steps"]]


def test_ci_slow_job_follows_fast_and_runs_the_full_check():
    slow = load("ci.yml")["jobs"]["slow"]
    assert slow["needs"] == "fast"
    assert "timeout 3600 scripts/definition-of-done.sh" in [s.get("run") for s in slow["steps"]]
    text = "\n".join(runs(load("ci.yml")))
    assert "du -sh warehouse" in text and "du -sh site" in text
    assert "GITHUB_OUTPUT" in text


def test_ci_uses_a_placeholder_denylist_it_writes_itself():
    ci = (WORKFLOWS / "ci.yml").read_text()
    assert "C2C_DENYLIST_PATH" in ci
    assert "placeholder" in ci


@pytest.mark.parametrize("name", ["ci.yml", "pages.yml"])
def test_no_step_echoes_a_secret_or_reads_private_docs(name):
    workflow = load(name)
    for step in steps(workflow):
        text = "\n".join(str(v) for v in (step.get("run"), step.get("with"), step.get("env")) if v)
        assert "secrets." not in text, step
        assert "docs/" not in text, step
