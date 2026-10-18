#!/usr/bin/env bash
# Definition of done: the commands that must pass before a unit of work counts as done.
# Meant to run on a clean `git archive HEAD` extraction (committed state only), not the working tree.
# The build and test commands are appended as they land.
#
#   scripts/definition-of-done.sh [--fast]
#
# --fast is accepted now and changes nothing until there are tests to skip.
set -euo pipefail
cd "$(dirname "$0")/.."

case "${1:-}" in
  ""|--fast) ;;
  *) echo "usage: definition-of-done.sh [--fast]" >&2; exit 2 ;;
esac

fail() { echo "definition-of-done: $1" >&2; exit 1; }

bash -n scripts/*.sh scripts/pre-push tests/*.sh

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

echo "definition-of-done: passed"
