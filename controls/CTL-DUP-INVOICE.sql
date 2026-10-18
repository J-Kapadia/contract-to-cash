-- remediation: Remove the duplicate of invoice {invoice_id} line {line_no} from `invoices.csv` and reload.
-- stage: raw
-- No two invoice rows share (invoice_id, line_no). Runs on the loaded books, before any model is
-- built, so a re-imported line is reported here and never reaches a model test.

select
    invoice_id,
    line_no,
    count(*) as copies,
    list(_row_hash order by _row_hash) as _row_hash
from raw.invoices
group by invoice_id, line_no
having count(*) > 1
order by invoice_id, line_no
