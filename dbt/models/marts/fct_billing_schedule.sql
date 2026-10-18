-- The billing schedule of every source: the contracts' own (`actual`) now, forecast contracts later.
select
    source,
    as_of,
    "case",
    customer_id,
    contract_id,
    line_id,
    invoice_date,
    service_start,
    service_end,
    amount_cents
from {{ ref('int_billing_schedule') }}
