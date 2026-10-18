#!/usr/bin/env bash
# Definition of done: the commands that must pass before a unit of work counts as done.
# Meant to run on a clean `git archive HEAD` extraction (committed state only).
# The hygiene guard and the build and test commands replace this stub as they land.
#
#   scripts/definition-of-done.sh [--fast]
set -euo pipefail
cd "$(dirname "$0")/.."
bash -n scripts/*.sh
echo "definition-of-done: passed"
