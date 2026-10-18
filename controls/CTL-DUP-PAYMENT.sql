-- remediation: Remove the duplicate application {payment_id} to invoice {invoice_id} from `payments.csv` and reload.
-- stage: raw
-- No two payment rows share (payment_id, invoice_id). A payment may be applied to several invoices,
-- but to each of them once.

select
    payment_id,
    invoice_id,
    count(*) as copies,
    list(_row_hash order by _row_hash) as _row_hash
from raw.payments
group by payment_id, invoice_id
having count(*) > 1
order by payment_id, invoice_id
