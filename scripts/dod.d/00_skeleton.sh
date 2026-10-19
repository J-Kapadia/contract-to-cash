#!/usr/bin/env bash
# The skeleton end to end: generate, close, explain a number, write the memo and find a number id in it.
uv run c2c gen --fixture small
# A close needs a sign-off of the configuration in force. The scratch sign-off below stands in
# until a reviewed fixture configuration exists; the committed fixture sign-off replaces this once
# the reviewed fixture config exists.
signoff_dir=$(mktemp -d)
uv run python -c 'from c2c.signoff import write_for; write_for("config", __import__("sys").argv[1], "dod scratch (synthetic)", "finance_lead", "2026-01-01")' "$signoff_dir"
C2C_SIGNOFF_DIR="$signoff_dir" uv run c2c close --month 2026-03 --fixture small --config config --json >/dev/null
rm -rf "$signoff_dir"
uv run c2c explain ar/2026-03/acct_0001/closing --fixture small >/dev/null
memo_out=$(mktemp -d)
uv run c2c memo --month 2026-03 --fixture small --out "$memo_out"
grep -rq data-num-id "$memo_out"
rm -rf "$memo_out"
