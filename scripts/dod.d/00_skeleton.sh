#!/usr/bin/env bash
# The skeleton end to end: generate, close, explain a number, write the memo and find a number id in it.
# The fast tier runs it on the committed `mini` books (generated into a scratch directory, so the
# committed files are never rewritten); the full tier runs it on the generated `small` books.
if [ "${fast:-0}" -eq 1 ]; then
  fixture=mini
  month=2026-03
  config_dir=${fixture_config:?}
  # `gen --verify` only compares the committed files with their manifest and writes nothing, so
  # generate into a scratch directory and verify what was written.
  gen_out=$(mktemp -d)
  uv run c2c gen --fixture mini --out "$gen_out"
  uv run c2c gen --fixture mini --out "$gen_out" --verify
  rm -rf "$gen_out"
  warehouse_args=(--warehouse "${work:?}/mini.duckdb")
else
  fixture=small
  month=2026-03
  config_dir=config
  uv run c2c gen --fixture small
  warehouse_args=()
fi
# A close needs a sign-off of the configuration in force. The scratch sign-off below stands in
# until a reviewed fixture configuration exists; the committed fixture sign-off replaces this once
# the reviewed fixture config exists.
signoff_dir=$(mktemp -d)
uv run python -c 'from c2c.signoff import write_for; write_for(__import__("sys").argv[2], __import__("sys").argv[1], "dod scratch (synthetic)", "finance_lead", "2026-01-01")' "$signoff_dir" "$config_dir"
C2C_SIGNOFF_DIR="$signoff_dir" uv run c2c close --month "$month" --fixture "$fixture" --config "$config_dir" ${warehouse_args[@]+"${warehouse_args[@]}"} --json >/dev/null
rm -rf "$signoff_dir"
uv run c2c explain ar/2026-03/acct_0001/closing --fixture "$fixture" ${warehouse_args[@]+"${warehouse_args[@]}"} >/dev/null
memo_out=$(mktemp -d)
uv run c2c memo --month "$month" --fixture "$fixture" ${warehouse_args[@]+"${warehouse_args[@]}"} --out "$memo_out"
grep -rq data-num-id "$memo_out"
rm -rf "$memo_out"
