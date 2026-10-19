#!/usr/bin/env bash
# Definition of done: the commands that must pass before a unit of work counts as done.
# Meant to run on a clean `git archive HEAD` extraction (committed state only), not the working tree.
# The product commands are fragments in scripts/dod.d/; the hygiene guard, bootstrap and tests stay here.
#
#   scripts/definition-of-done.sh [--fast]
#
# --fast skips the tests marked `slow`.
# Exit 2 means the toolchain could not be bootstrapped (no uv, and pip could not install it).
set -euo pipefail
cd "$(dirname "$0")/.."

start=$SECONDS
fast=0
case "${1:-}" in
  "") ;;
  --fast) fast=1 ;;
  *) echo "usage: definition-of-done.sh [--fast]" >&2; exit 2 ;;
esac

fail() { echo "definition-of-done: $1" >&2; exit 1; }

bash -n scripts/*.sh scripts/dod.d/*.sh scripts/pre-push tests/*.sh

# Hygiene guard: generated data and build output must stay out of git, and so must secrets.
# The exact line must be in .gitignore; where .git exists git must agree.
ignored=(warehouse/ data/books/ data/truth/ site/ dbt/target/ dbt/logs/ .venv/)
for d in "${ignored[@]}"; do
  grep -Fxq "$d" .gitignore || fail ".gitignore lacks the line $d"
  if [ -e .git ]; then
    git check-ignore -q "$d" || fail "$d is not ignored"
  fi
done
for p in '*.env' '.env.*' '*.pem' '*.key'; do
  grep -Fxq "$p" .gitignore || fail ".gitignore lacks the line $p"
done
for p in /docs/plan/ /docs/notes/ /docs/research/ /docs/build-log/ /notes/ /AGENTS.md /CLAUDE.md /.needle.yaml /.beads/; do
  grep -Fxq "$p" .gitignore || fail ".gitignore lacks the line $p"
done

# No private path is tracked (needs the repository; a bare extraction has nothing to list).
if [ -e .git ]; then
  if git ls-files | grep -E '^(docs/(plan|notes|research|build-log)/|notes/|AGENTS\.md$|CLAUDE\.md$|\.needle\.yaml$|\.beads/)'; then
    fail "private paths are tracked (listed above)"
  fi
fi

# The denylist lives outside every repository; without it the full scrub cannot run.
deny=${C2C_DENYLIST_PATH:-$HOME/.needle/contract-to-cash.denylist}
if [ ! -f "$deny" ] || ! grep -Evq '^[[:space:]]*(#|$)' "$deny"; then
  echo "definition-of-done: denylist missing" >&2
  exit 1
fi

scripts/scrub-check.sh --secrets-only
bash tests/test_scrub_check.sh

# Bootstrap: this runs on a bare `git archive` extraction where nothing is installed.
UV_PIN="0.13.0"
export PATH="$HOME/.local/bin:$PATH"
if ! command -v uv >/dev/null 2>&1; then
  pip install --user "uv==$UV_PIN" >/dev/null 2>&1 || python3 -m pip install --user "uv==$UV_PIN" >/dev/null 2>&1 || true
  hash -r
fi
if ! command -v uv >/dev/null 2>&1; then
  echo "definition-of-done: uv is not installed and 'pip install --user uv==$UV_PIN' failed" >&2
  exit 2
fi

uv sync --frozen
uv lock --check
uv run c2c --version

# The product commands live in numbered fragments under scripts/dod.d/, run in name order so each
# new command lands as a file of its own. A fragment gets `fast`, a shared scratch directory `work`
# and `fixture_config`, and runs from the repository root.
export fast
export work
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT
export fixture_config=tests/fixtures/config
for fragment in scripts/dod.d/[0-9][0-9]_*.sh; do
  [ -e "$fragment" ] || break
  echo "definition-of-done: running $fragment"
  bash -euo pipefail "$fragment" || fail "fragment $fragment failed"
done

# Only c2c.db opens a DuckDB connection (the test wrapper that applies the same caps is the one other place).
if grep -rn 'duckdb\.connect(' src tests --include='*.py' | grep -v -e '^src/c2c/db\.py:' -e '^tests/conftest\.py:'; then
  fail "duckdb.connect( outside c2c.db (listed above): open connections through c2c.db.connect"
fi

# Nothing to collect (pytest would exit 5) until the first test file lands.
if [ -n "$(find tests -name 'test_*.py' -print -quit 2>/dev/null)" ]; then
  if [ "$fast" -eq 1 ]; then
    uv run pytest -q -m "not slow"
  else
    uv run pytest -q
  fi
fi

echo "definition-of-done: passed in $((SECONDS - start))s (fast=$fast)"
