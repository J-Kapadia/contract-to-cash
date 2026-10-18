-- remediation: Row {row_ref} refers to {ref_kind} {ref_id} that does not exist: correct the reference or add the missing row.
-- stage: raw
-- Every contract, customer, contract line, bank transaction and invoice that a row refers to is
-- in the loaded books, and every bank row has a posting date. One row per broken reference.

with broken as (
    select 'contract_lines:' || l.contract_id || '/' || l.version || '/' || l.line_id as row_ref,
           'contract version' as ref_kind,
           l.contract_id || '/' || l.version as ref_id,
           l._row_hash
    from raw.contract_lines as l
    where not exists (select 1 from raw.contracts as c where c.contract_id = l.contract_id and c.version = l.version)

    union all
    select 'invoices:' || i.invoice_id || '/' || i.line_no, 'contract', i.contract_id, i._row_hash
    from raw.invoices as i
    where not exists (select 1 from raw.contracts as c where c.contract_id = i.contract_id)

    union all
    select 'invoices:' || i.invoice_id || '/' || i.line_no, 'contract line', i.contract_line_id, i._row_hash
    from raw.invoices as i
    where not exists (select 1 from raw.contract_lines as l where l.line_id = i.contract_line_id)

    union all
    select 'invoices:' || i.invoice_id || '/' || i.line_no, 'billing customer', i.billing_customer_id, i._row_hash
    from raw.invoices as i
    where not exists (select 1 from raw.billing_customers as b where b.billing_customer_id = i.billing_customer_id)

    union all
    select 'payments:' || p.payment_id || '/' || p.invoice_id, 'billing customer', p.billing_customer_id, p._row_hash
    from raw.payments as p
    where not exists (select 1 from raw.billing_customers as b where b.billing_customer_id = p.billing_customer_id)

    union all
    select 'payments:' || p.payment_id || '/' || p.invoice_id, 'bank transaction', p.bank_txn_id, p._row_hash
    from raw.payments as p
    where not exists (select 1 from raw.bank_transactions as t where t.bank_txn_id = p.bank_txn_id)

    union all
    select 'payments:' || p.payment_id || '/' || p.invoice_id, 'invoice', p.invoice_id, p._row_hash
    from raw.payments as p
    where not exists (select 1 from raw.invoices as i where i.invoice_id = p.invoice_id)

    union all
    select 'bank_transactions:' || t.bank_txn_id, 'posting date of bank transaction', t.bank_txn_id, t._row_hash
    from raw.bank_transactions as t
    where coalesce(trim(t.posted_on), '') = ''
)

select
    row_ref,
    ref_kind,
    ref_id,
    [_row_hash] as _row_hash
from broken
order by row_ref, ref_kind, ref_id
