-- remediation: Contract {contract_id} version {version} line {line_id} has line type {product}, which no billing rule bills: set it to one of {billed_types}, or add a billing rule and list the type here.
-- stage: raw
-- A contract line whose product no billing rule bills is left out of the billing schedule, so the invoices issued
-- for it are reported as unexpected only later. The check runs on the loaded books before any model is built.

with billed as (
    -- the line types the billing schedule model bills; this list changes with that model
    select * from (values ('subscription'), ('overage')) as t (product)
)

select
    l.contract_id,
    l.version,
    l.line_id,
    l.product,
    (select string_agg(product, ', ' order by product desc) from billed) as billed_types,
    [l._row_hash] as _row_hash
from raw.contract_lines as l
where coalesce(trim(l.product), '') = ''
    or l.product not in (select product from billed)
order by l.contract_id, l.version, l.line_id
