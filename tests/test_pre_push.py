"""The pre-push hook prints one line when it accepts a push, and still refuses private material
and an empty denylist."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from conftest import ROOT

URL = "https://example.invalid/public/repo.git"
ACCEPTED = f"pre-push: checked 1 commit(s) to {URL}: nothing private, no denylisted term"


def git(repo: Path, *args: str) -> str:
    done = subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid", *args],
        cwd=repo, capture_output=True, text=True, check=True,
    )
    return done.stdout.strip()


def commit(repo: Path, path: str, text: str) -> str:
    target = repo / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")
    git(repo, "add", path)
    git(repo, "commit", "-q", "-m", f"add {path}")
    return git(repo, "rev-parse", "HEAD")


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    git(root, "init", "-q", "-b", "main")
    shutil.copytree(ROOT / "scripts", root / "scripts")
    git(root, "add", "scripts")
    git(root, "commit", "-q", "-m", "scripts")
    return root


def push(repo: Path, tmp_path: Path, old: str, new: str, terms: str | None = "zzprivateterm\n"):
    deny = tmp_path / "denylist"
    if terms is not None:
        deny.write_text(terms, encoding="utf-8")
    return subprocess.run(
        ["bash", "scripts/pre-push", "origin", URL],
        cwd=repo, input=f"refs/heads/main {new} refs/heads/main {old}\n",
        capture_output=True, text=True, env={"PATH": "/usr/bin:/bin", "HOME": str(tmp_path), "C2C_DENYLIST_PATH": str(deny)},
    )


def test_clean_commit_is_accepted_with_one_line(repo, tmp_path):
    old = git(repo, "rev-parse", "HEAD")
    new = commit(repo, "src/readme.txt", "hello\n")
    done = push(repo, tmp_path, old, new)
    assert done.returncode == 0, done.stderr
    assert done.stderr.splitlines() == [ACCEPTED]


def test_private_path_is_refused_without_acceptance_line(repo, tmp_path):
    old = git(repo, "rev-parse", "HEAD")
    new = commit(repo, "docs/plan/outline.md", "hello\n")
    done = push(repo, tmp_path, old, new)
    assert done.returncode != 0
    assert "nothing private" not in done.stderr
    assert "docs/plan/outline.md" in done.stderr


def test_empty_denylist_is_refused(repo, tmp_path):
    old = git(repo, "rev-parse", "HEAD")
    new = commit(repo, "src/readme.txt", "hello\n")
    done = push(repo, tmp_path, old, new, terms="")
    assert done.returncode != 0
    assert "nothing private" not in done.stderr


def test_deleting_a_ref_checks_zero_commits(repo, tmp_path):
    old = git(repo, "rev-parse", "HEAD")
    done = push(repo, tmp_path, old, "0" * 40)
    assert done.returncode == 0, done.stderr
    assert done.stderr.splitlines() == [f"pre-push: checked 0 commit(s) to {URL}: nothing private, no denylisted term"]
