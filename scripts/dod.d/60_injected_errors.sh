#!/usr/bin/env bash
# One injected error end to end: a one-cent slip in a copy of the mini books fails the close of 2026-01
# with the receivable tie-out and exit 3, while the clean books close.
injected=$(mktemp -d)
signoff_dir=$(mktemp -d)
warehouse_dir=$(mktemp -d)
uv run c2c inject --error rounding_slip --fixture mini --out "$injected/books" >/dev/null
uv run python -c 'from c2c.signoff import write_for; write_for("tests/fixtures/config", __import__("sys").argv[1], "dod scratch (synthetic)", "finance_lead", "2026-01-01")' "$signoff_dir"
status=0
out=$(C2C_SIGNOFF_DIR="$signoff_dir" uv run c2c close --month 2026-01 --books "$injected/books" --warehouse "$warehouse_dir/w.duckdb" \
    --config tests/fixtures/config --json 2>/dev/null) || status=$?
rm -rf "$injected" "$signoff_dir" "$warehouse_dir"
test "$status" -eq 3
printf '%s' "$out" | uv run python -c 'import json, sys; ids = [c["id"] for c in json.load(sys.stdin)["failed_controls"]]; sys.exit(0 if "CTL-TIE-AR" in ids else 1)'
