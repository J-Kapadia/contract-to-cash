-- remediation: Apply bank receipt {bank_txn_id} ({amount_cents} cents, {counterparty_text}) to an invoice in `payments.csv`, or add the payer to the customers.
-- stage: mart
-- Every bank receipt is applied by a payment row, or auto-applied because its counterparty is a
-- customer of the master and its amount equals exactly one open invoice of that customer. Reports
-- each receipt that is neither.

select
    cash.bank_txn_id,
    bank.amount_cents,
    bank.counterparty_text,
    bank.posted_on,
    [bank._row_hash] as _row_hash
from int.int_cash_application as cash
join int.stg_bank_transactions as bank using (bank_txn_id)
where cash.kind = 'unmatched'
order by cash.bank_txn_id
