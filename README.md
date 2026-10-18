# contract-to-cash

From signed contract to cash, tied to the general ledger to the cent. One command a month closes and locks a month of a B2B software company's books: the deferred revenue, accounts receivable and cash roll-forwards tie to the ledger, a 13-week cash forecast runs in three cases with its backtested error, and a memo is written in which every number opens an `explain` chain that ends at hashed source rows.

Everything is synthetic. A seeded generator writes the books of "Northwind Labs (synthetic)" and a hidden answer key; the workflow reads only the books, and `close --check` proves its results against the key. Revenue recognition follows the public standard ASC 606 and its IFRS twin, IFRS 15. The project makes no claim about any real company's policies or numbers.

## Status

What runs today, on the synthetic books that `c2c gen` writes: `c2c close --month M` loads the books, builds the marts (billing schedule, revenue, the deferred revenue, accounts receivable and cash roll-forwards, customer matching and the customer master, and the revenue-to-cash bridge), runs eight controls and writes a provenance record for every number it produces; `c2c explain <id>` walks a number back to its hashed source rows; `c2c memo` renders the close memo from those records; `c2c inject --error <name>` plants one of seven known errors (`duplicate_customer`, `wrong_period`, `misapplied_payment`, `unbilled_amendment`, `defrev_gl_slip`, `orphan_bank_txn`, `rounding_slip`) in a copy of the books so a control can be shown catching it; `c2c signoff` records the reviewer's sign-off.

The eight controls: `CTL-DUP-INVOICE`, `CTL-DUP-PAYMENT` and `CTL-REF-INTEGRITY` check the loaded books before dbt starts; `CTL-BILL-CONTRACT`, `CTL-CUTOFF`, `CTL-MASTER-ANCHOR`, `CTL-MASTER-REVIEW` and `CTL-TIE-AR` check the marts. In flight: `CTL-CASH-APP` and `CTL-BANK-UNMATCHED` (cash application), `CTL-TIE-DEFREV`, `CTL-TIE-CASH` and `CTL-TIE-REV` (the remaining ledger ties) and `CTL-LOCK` (locked months). Not built yet: `close --check` against the answer key, the catch-up close of earlier months, the 13-week cash forecast and its backtest, and `memo --check`.
