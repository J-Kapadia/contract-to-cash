#!/usr/bin/env bash
# Installs scripts/pre-push as this clone's pre-push hook (a symlink, so the hook tracks the
# script). Run once per clone.
#
#   scripts/install-hooks.sh
set -euo pipefail
cd "$(dirname "$0")/.."

if ! git rev-parse --git-dir >/dev/null 2>&1; then
  echo "install-hooks: not a git repository" >&2
  exit 1
fi
hook=$(git rev-parse --path-format=absolute --git-path hooks/pre-push)
mkdir -p "$(dirname "$hook")"
ln -sf "$PWD/scripts/pre-push" "$hook"
echo "install-hooks: $hook -> $PWD/scripts/pre-push"
