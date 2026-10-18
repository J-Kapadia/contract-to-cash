#!/usr/bin/env bash
# Tests for scripts/scrub-check.sh and scripts/pre-push, run on scratch directories and
# scratch repositories. The fake secrets and private terms are assembled at run time so this
# file itself stays clean.
#
#   tests/test_scrub_check.sh
set -uo pipefail

root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT
failures=0

check() {  # check NAME EXPECTED_EXIT ACTUAL_EXIT
  if [ "$2" -ne "$3" ]; then
    echo "FAIL: $1 (expected exit $2, got $3)" >&2
    failures=$((failures + 1))
  else
    echo "ok: $1"
  fi
}

fake_token="ghp_$(printf 'a1B2c3D4%.0s' 1 2 3 4 5)"
term=$(printf 'Gla%s' 'mdring')           # stands in for a private company name
plan_ref=$(printf 'NEE%s' 'DLE')          # matches a private-terms pattern
denylist="$work/denylist"
printf '# synthetic denylist\n%s\n' "$term" > "$denylist"
export C2C_DENYLIST_PATH="$denylist"

# --- scrub-check --secrets-only on directories
mkdir "$work/clean" "$work/dirty"
echo "nothing to see" > "$work/clean/a.txt"
printf 'token = %s\n' "$fake_token" > "$work/dirty/b.txt"
"$root/scripts/scrub-check.sh" --secrets-only "$work/clean" >/dev/null; check "clean directory passes" 0 $?
out=$("$root/scripts/scrub-check.sh" --secrets-only "$work/dirty"); rc=$?
check "directory with a token-shaped string fails" 1 $rc
case "$out" in *"$fake_token"*) check "output never prints the match" 0 1 ;; *) check "output never prints the match" 0 0 ;; esac
case "$out" in *b.txt:1*) check "output names file:line" 0 0 ;; *) check "output names file:line" 0 1 ;; esac

# --- scrub-check full mode needs the denylist
(cd "$root" && C2C_DENYLIST_PATH="$work/absent" scripts/scrub-check.sh >/dev/null 2>&1); check "full mode without denylist exits 2" 2 $?
: > "$work/empty"
(cd "$root" && C2C_DENYLIST_PATH="$work/empty" scripts/scrub-check.sh >/dev/null 2>&1); check "full mode with empty denylist exits 2" 2 $?

# --- pre-push in a scratch repository
repo="$work/repo"
git init -q "$repo"
cp -r "$root/scripts" "$repo/scripts"
cd "$repo" || exit 1
git config user.email t@example.com; git config user.name t
git add scripts && git commit -qm "chore: scripts" && base=$(git rev-parse HEAD)
zero=0000000000000000000000000000000000000000

push_check() {  # push_check NAME EXPECTED_EXIT [env assignments...]; pushes base..HEAD
  local name=$1 want=$2; shift 2
  printf 'refs/heads/main %s refs/heads/main %s\n' "$(git rev-parse HEAD)" "$base" \
    | env "$@" scripts/pre-push origin url >/dev/null 2>"$work/err"
  check "$name" "$want" $?
}

echo "fine" > ok.txt && git add ok.txt
git commit -qm "feat: add a file" -m "Bead-Id: x-1"
push_check "clean commit with a Bead-Id trailer is allowed" 0 C2C_DENYLIST_PATH="$denylist"
push_check "absent denylist refuses" 1 C2C_DENYLIST_PATH="$work/absent"
grep -q 'denylist missing' "$work/err"; check "absent denylist says so" 0 $?

mkdir notes && echo x > notes/x.md && git add -f notes/x.md && git commit -qm "chore: add notes"
push_check "private path refuses" 1 C2C_DENYLIST_PATH="$denylist"
git rm -q -f notes/x.md && git commit -qm "chore: drop notes"
git reset -q --hard "$base"; echo fine > ok.txt; git add ok.txt

echo "see $term for details" > t.txt && git add t.txt && git commit -qm "docs: add a file"
push_check "denylist term in a file refuses" 1 C2C_DENYLIST_PATH="$denylist"
grep -q "$term" "$work/err"; check "refusal never prints the term" 1 $?
git reset -q --hard "$base"

echo ok > ok.txt && git add ok.txt && git commit -qm "feat: ref to $plan_ref"
push_check "private term pattern in a message refuses" 1 C2C_DENYLIST_PATH="$denylist"
git reset -q --hard "$base"

# --- install-hooks links the hook
scripts/install-hooks.sh >/dev/null; [ -L .git/hooks/pre-push ] && [ -x .git/hooks/pre-push ]; check "install-hooks symlinks the hook" 0 $?

if [ "$failures" -ne 0 ]; then echo "test_scrub_check: $failures failure(s)" >&2; exit 1; fi
echo "test_scrub_check: passed"
