# contract-to-cash

From signed contract to cash, tied to the general ledger to the cent. One command a month closes and locks a month of a B2B software company's books: the deferred revenue, accounts receivable and cash roll-forwards tie to the ledger, a 13-week cash forecast runs in three cases with its backtested error, and a memo is written in which every number opens an `explain` chain that ends at hashed source rows.

Everything is synthetic. A seeded generator writes the books of "Northwind Labs (synthetic)" and a hidden answer key; the workflow reads only the books, and `close --check` proves its results against the key. Revenue recognition follows the public standard ASC 606 and its IFRS twin, IFRS 15. The project makes no claim about any real company's policies or numbers.

## Run it on the fixture

The repository carries a small set of books, the `mini` fixture (18 months, October 2024 to March 2026), and a configuration to close it with. You need [uv](https://docs.astral.sh/uv/) and Python 3.12.

```sh
uv sync --frozen
scratch=$(mktemp -d)
```

**Generate and verify the books.** `gen` writes the books and the answer key into a directory; `--verify` checks them against their manifest.

```sh
uv run c2c gen --fixture mini --out "$scratch/gen"
uv run c2c gen --fixture mini --out "$scratch/gen" --verify
```

```
wrote 25 files for mini (seed 1) under <dir>
CHK-GEN-VERIFY: mini books match the manifest
```

**Sign the configuration.** A close needs a sign-off of the configuration in force. `c2c signoff` asks a person to read the file hashes and confirm in a terminal; for a scripted run on the synthetic fixture, write a scratch sign-off the way `scripts/dod.d/00_skeleton.sh` does and point `C2C_SIGNOFF_DIR` at it.

```sh
uv run python -c 'from c2c.signoff import write_for; write_for(__import__("sys").argv[2], __import__("sys").argv[1], "readme scratch (synthetic)", "finance_lead", "2026-01-01")' "$scratch/signoff" tests/fixtures/config
export C2C_SIGNOFF_DIR="$scratch/signoff"
```

The commands below take `--fixture mini`, `--config tests/fixtures/config` and a `--warehouse` file, so that nothing is written outside `$scratch`. The warehouse is a DuckDB file; the first close builds it.

**Close a month.** `close --month 2026-03` loads the books through that month, runs the models once and closes every month through it in order (2024-10, 2024-11, and so on), running the fourteen controls on each. It takes a couple of minutes on the fixture.

```sh
uv run c2c close --month 2026-03 --fixture mini --config tests/fixtures/config --warehouse "$scratch/mini.duckdb" --json
```

```
closed 2026-03: loaded 3995 rows from 22 files, dbt build passed, 14 controls passed
```

The `--json` document carries `"close_status": "closed"`, one entry per control with its status and violating rows, and the `result_hash` of the month. A closed month is locked: if its loaded inputs later change, `CTL-LOCK` fails the next close.

**Check the close against the answer key.** `close --check` compares a closed month with the answer key and re-derives every provenance digest.

```sh
uv run c2c close --month 2026-03 --check --fixture mini --config tests/fixtures/config --warehouse "$scratch/mini.duckdb"
```

```
2026-03 is closed and unchanged; matches the answer key
```

**See the months.** `status` lists every month with its state and result hash, and the forecast runs by as-of date.

```sh
uv run c2c status --fixture mini --config tests/fixtures/config --warehouse "$scratch/mini.duckdb"
```

```
months:
  2024-10  closed  d9a77b2f0ae3b6495f67f6f36dfee2d8b371e486c11ddd67fbefe30484115ad7
  ...
  2026-03  closed  c14a7425e9d3a3de250762420ff3099fc8064e220a08a6b9fd1b41e11f91536d
forecast runs: none
```

**Explain a number.** Every number has an id; `explain` prints its value, the step that produced it, its digest, the controls that passed, and the source rows behind it.

```sh
uv run c2c explain ar/2026-03/acct_0001/closing --fixture mini --warehouse "$scratch/mini.duckdb"
```

```
ar/2026-03/acct_0001/closing
  value:   275000 cents
  step:    fct_ar_rollforward
  digest:  6f5f31d0522496cc45877b660f4922c3edca390181a67a620c24951ca5f18cd2 over 15 row(s), re-derived matches
  ...
  invoices (8 row(s)): invoice_id, line_no, billing_customer_id, ...
    inv_000095 | 1 | bill_0001 | ctr_0001_01 | ctr_0001_01_L2 | 2026-03-31 | ... | overage | 275000 | ...
```

`--depth` sets how far to walk. `--depth 0` prints the record only; `--depth 1` (the default) lists a leaf's source rows or a composite's children; each further level walks the children one level deeper. The revenue-to-cash bridge line `bridge/2026-03/receipts` is a composite of 53 children, among them the receivable closings:

```sh
uv run c2c explain bridge/2026-03/receipts --depth 2 --fixture mini --warehouse "$scratch/mini.duckdb"
```

```
bridge/2026-03/receipts
  value:   15125000 cents
  step:    fct_bridge
  digest:  1df7608d1341f7964f81ba903d38cb047464e9367227863416e5d2d8e1730713 over 53 child(ren), re-derived matches
```

**Write the memo.** `memo` writes `OUT/2026-03/index.html`, with the id of each number on the page in a `data-num-id` attribute. It refuses a month that is not closed.

```sh
uv run c2c memo --month 2026-03 --fixture mini --config tests/fixtures/config --warehouse "$scratch/mini.duckdb" --out "$scratch/memo"
uv run c2c memo --month 2026-04 --fixture mini --config tests/fixtures/config --warehouse "$scratch/mini.duckdb" --out "$scratch/memo"
```

```
wrote <dir>/memo/2026-03/index.html
month 2026-04 is not closed: run `c2c close --month 2026-04` first.
```

The second command exits 4.

**Plant an error and watch a control catch it.** `inject` copies the books to an empty directory with one named error planted in them. The seven names are `duplicate_customer`, `wrong_period`, `misapplied_payment`, `unbilled_amendment`, `defrev_gl_slip`, `orphan_bank_txn` and `rounding_slip`.

```sh
uv run c2c inject --error rounding_slip --fixture mini --out "$scratch/inj"
```

```
wrote <dir>/inj with rounding_slip planted: 1 row(s) changed.
```

Closing 2026-01 of that copy fails, exit 3, and the month stays open:

```sh
uv run c2c close --month 2026-01 --books "$scratch/inj" --warehouse "$scratch/inj.duckdb" --config tests/fixtures/config
```

```
cannot close 2026-01: 2026-01 stays open: CTL-TIE-AR failed.
CTL-TIE-AR: 1 violating row(s)
  - The roll-forward differs from GL 1200 by 1 cents in 2026-01: review GL entries je_000345, ...
```

`inject --all` plants all seven in one copy, each in its own month, the seven months that end with the last month of the books:

```sh
uv run c2c inject --all --fixture mini --out "$scratch/injall"
```

```
wrote <dir>/injall with 7 errors planted, one in each of 7 months.
```

**Check the customer master.** `master --check` builds the customer master through a month and compares its matches with the answer key.

```sh
uv run c2c master --month 2026-03 --check --fixture mini --config tests/fixtures/config --warehouse "$scratch/mini.duckdb"
```

```
built the customer master for 2026-03: 19 clusters, 122 candidate pairs; precision 10000 bps and recall 10000 bps automatic, 10000 and 10000 after review
```

**Check the installation.** `doctor` exits 0 when healthy, 1 on warnings and 2 on failures.

```sh
uv run c2c doctor --fixture mini --config tests/fixtures/config --warehouse "$scratch/mini.duckdb"
```

```
doctor: healthy
ok    python                3.12.15 (CPython)
...
ok    signoff               signed by readme scratch (synthetic) (finance_lead) on 2026-01-01
```

## Commands

`uv run c2c --help` lists the commands; `uv run c2c <command> --help` lists the options of each. Every command that reads the books takes `--books`, `--truth`, `--warehouse`, `--config` and the preset `--fixture` (also set by `C2C_BOOKS`, `C2C_TRUTH`, `C2C_WAREHOUSE` and `C2C_CONFIG_DIR`), and `--json` for machine output.

| Command | What it does |
| --- | --- |
| `c2c close` | Close `--month`: load the books through it, run the models once, close each month up to it in order, run the controls and write a provenance record for every number. `--check` compares a closed month with the answer key and re-derives every provenance digest. Exit 3 when a control fails. |
| `c2c doctor` | Report the health of the installation: versions, the books and their manifest, the sign-off, the warehouse and its lock, the reviewed customer pairs. Exit 0 healthy, 1 warnings, 2 failures. |
| `c2c explain` | Print the provenance of a number id: its value, step, digest and source rows; `--depth N` walks composites to their leaves. |
| `c2c gen` | Generate the books and the answer key from a seed, or with `--verify` check them against the manifest. `--fixture mini` or `small` selects a preset. |
| `c2c inject` | Copy the books to `--out` with one named error planted (`--error`), or all seven answer-key errors, one per month (`--all`). |
| `c2c master` | Build the customer master through `--month` and report its clusters, candidate pairs and scores; `--check` compares the matches with the answer key. |
| `c2c memo` | Write the memo for a closed `--month` to `OUT/MONTH/index.html`; refuses a month that is not closed. |
| `c2c signoff` | Sign the files in the configuration directory. A person confirms it in a terminal; without one it exits 4. |
| `c2c status` | List the months with their state and result hash, and the forecast runs by as-of date. |

## Controls

The controls are the SQL files in `controls/`. A close runs the `raw` ones on the loaded books before any model is built and the `mart` ones on the marts. Each reports violating rows; one or more fails the close, and the failure message says what to correct. In file-name order:

- `CTL-BANK-UNMATCHED`: every bank receipt is applied by a payment, or auto-applied to the one open invoice of a customer it matches by amount. Catches `orphan_bank_txn`.
- `CTL-BILL-CONTRACT`: the billing schedule of the contracts equals the invoices issued, by contract line and service period, to the cent. Catches `unbilled_amendment`.
- `CTL-CASH-APP`: a payment, the invoice it is applied to and the bank counterparty of its receipt all resolve to the same customer. Catches `misapplied_payment`.
- `CTL-CUTOFF`: every ledger entry of an invoice sits in the month of the invoice's date. Catches `wrong_period`.
- `CTL-DRIFT-ACCOUNT`: every GL account on a ledger line has a row with a name and a kind in `data/reference/gl_accounts.csv`; an account the reference does not map fails the close before any model is built, until it is added there or corrected on the lines. Catches `unmapped_account`.
- `CTL-DRIFT-LINE-TYPE`: every contract line is of a type the billing schedule bills (`subscription` or `overage`); a line of any other type fails the close before any model is built, until its type is corrected or a billing rule and the type are added.
- `CTL-DRIFT-SCHEMA`: each books file has the columns it had when the latest earlier month was closed; a column added, dropped or renamed fails the close until the file is restored or that month is closed again on the new layout.
- `CTL-DUP-INVOICE`: no two invoice rows share an invoice id and line number.
- `CTL-DUP-PAYMENT`: no two payment rows share a payment id and invoice id.
- `CTL-LOCK`: a locked month's loaded rows still hash to the digest recorded when it was locked.
- `CTL-MASTER-ANCHOR`: each customer cluster has exactly one CRM account.
- `CTL-MASTER-REVIEW`: every pair of customer-name variants in the doubt band has a reviewed `same` or `different` decision in `customer_matches.yaml`. Catches `duplicate_customer`.
- `CTL-REF-INTEGRITY`: every contract, customer, contract line, bank transaction and invoice that a row refers to exists, and every bank row has a posting date.
- `CTL-TIE-AR`: accounts receivable equals GL 1200. Catches `rounding_slip`.
- `CTL-TIE-CASH`: cash equals GL 1000.
- `CTL-TIE-DEFREV`: deferred revenue equals GL 2400. Catches `defrev_gl_slip`.
- `CTL-TIE-REV`: recognised revenue equals GL 4000 and 4100 activity, month by month.

Each of the seven injected errors is caught by the control named against it above, alone and in the combined copy.

`close --check` also runs `CTL-PROV`, the one control written in Python rather than SQL: it re-derives the digest of every provenance record of the closed month from its source rows and fails on a record that no longer matches.

## The forecast

The 13-week cash forecast is a set of dbt models in `dbt/models/forecast/`, built as of a date from point-in-time views over the books (only rows known on or before that date) and the closed marts. It holds three cases, `base`, `downside` and `upside`, whose drivers are set in the `cases` block of `policy.yaml`:

- `fc_cash_13w` is the roll, weeks `W01` to `W13` from the day after the as-of date. Each row is a case, a week and a line: cash in from `open_ar`, `contracted`, `renewal`, `expansion` and `new`; cash out for `payroll`, `commissions`, `cloud`, `vendors`, `events` and `rent`. Every row carries the week's opening and ending balance; `W01` opens at the closing cash of the as-of date's month, and ending = opening + cash in − cash out.
- `fc_alerts` is the minimum-cash alert: for each case, the first week whose ending cash falls below `min_cash_cents` of the policy, and by how much it falls short. A case that never falls below it has no row.
- `fc_revenue` is the 12-month revenue by source (`contracted`, `renewal`, `expansion`, `new`).

`close` leaves the models tagged `forecast` out of its build, the memo renders the receivable position by customer, and `status` lists the forecast runs recorded in the warehouse (none on the fixture). The forecast models are exercised by their tests (`tests/test_forecast_*.py`, `tests/test_cash_out.py`, `tests/test_expected_cash.py`). The forecast and its backtest have number ids of their own (`fc/` and `bt/`, below), so that each forecast number opens an `explain` chain like any other.

## Numbers and provenance

Every number the workflow reports has an id, and every id opens an `explain` chain. A leaf number lists the hashed source rows it was computed from; a composite lists the numbers it adds up, which `--depth` walks down to leaves. `close` writes a record for each number (value, step, digest of its inputs, the controls that passed, the commit), `explain` re-derives the digest from the source rows and says whether it matches, and `close --check` does the same for every number of a closed month.

An id is `/`-separated. Its first segment is a family, the second the month (`YYYY-MM`) or as-of date it belongs to. The grammar is closed: `src/c2c/ids.py` parses an id against it, and an id outside it is refused. The families:

| Family | Shape | Numbers |
| --- | --- | --- |
| `cust` | `cust/<month>/<customer>/<members\|arr>` | customer master: members and annual recurring revenue |
| `rev` | `rev/<month>/<customer>/<subscription\|overage\|credit>` | recognised revenue by line type |
| `defrev` | `defrev/<month>/<customer>/<opening\|billings\|recognised\|closing>` | deferred revenue roll-forward |
| `ar` | `ar/<month>/<customer>/<opening\|billed\|collected\|credits\|closing>` | receivables roll-forward |
| `cash` | `cash/<month>/<opening\|receipts\|disbursements\|closing>` | cash roll-forward |
| `tie` | `tie/<month>/<gl account>` | ledger tie-out per account |
| `aging` | `aging/<month>/<bucket>` or `aging/<month>/<customer>/<bucket>` | receivables aging |
| `dtp` | `dtp/<month>/<customer or *>/<median\|p80>` | days to pay |
| `bridge` | `bridge/<month>/<revenue\|defrev_movement\|net_billings\|ar_movement\|receipts>` | revenue-to-cash bridge |
| `chg` | `chg/<month>/<arr_new\|arr_expansion\|arr_contraction\|arr_churn>` | ARR change |
| `rollup` | `rollup/<month>/<nrr_bps\|grr_bps\|logo_churn\|stale_opps>` | retention and pipeline roll-up |
| `master` | `master/<precision_auto\|recall_auto\|pairs_doubt\|pairs_reviewed>` | customer-matching quality |
| `fc` | `fc/<as_of>/<case>/cash/<week>/<line or balance>`, `.../rev/<month>/<source>`, `.../alert/first_breach_week` | forecast cash, revenue and alert |
| `rate` | `rate/<as_of>/<renewal\|expansion\|win>/<cohort or stage>` | measured renewal, expansion and win rates |
| `bt` | `bt/<as_of or summary>/<case>/<metric>` | forecast backtest errors |
| `ref` | `ref/<file>/<key>` | reference files |
| `cfg` | `cfg/<policy key>` | configuration values |

## Tests

```sh
uv run pytest -q -m "not slow"
uv run pytest -q
```

The first is the fast subset; the second adds the tests marked `slow`, which run against the full-size books or close injected copies. `scripts/definition-of-done.sh --fast` checks the shell scripts and the repository hygiene (generated data and secrets stay out of git; `scripts/scrub-check.sh`), bootstraps with `uv sync --frozen`, runs the product fragments in `scripts/dod.d/` on the `mini` fixture (generate and verify, close, explain a number, write the memo and find a number id in it), then runs `uv run pytest -q -m "not slow"`. `scripts/definition-of-done.sh` without `--fast` runs the same fragments on the generated `small` books, adds the injected-error close, and runs the whole suite.

## Layout

- `src/c2c/`: the `c2c` command line (`commands/`), the loader, the close, the explain and provenance records, the id grammar, the memo and its templates, the generator (`gen/`), the injected errors (`inject.py`, `drift/`).
- `dbt/`: the dbt project: staging, intermediate, mart and forecast models, and the macros.
- `controls/`: the controls, one SQL file each.
- `config/`: the generator settings, the policy and the reviewed customer matches for the full books; `tests/fixtures/config/` holds the ones for the `mini` fixture.
- `data/`: the reference files and the manifest of the generated books (the books themselves are generated, not committed).
- `tests/`: the tests; `tests/fixtures/` holds the committed `mini` books, answer key and expected results.
- `scripts/`: the definition of done and its fragments, the hygiene check and the git hooks.

## Licence

MIT; see `LICENSE`.
