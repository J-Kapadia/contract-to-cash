select
    invoice_id,
    line_no::integer as line_no,
    billing_customer_id,
    contract_id,
    contract_line_id,
    invoice_date::date as invoice_date,
    gl_period,
    due_date::date as due_date,
    service_start::date as service_start,
    service_end::date as service_end,
    line_type,
    amount_cents::bigint as amount_cents,
    known_on::date as known_on,
    _month,
    _row_hash
from {{ source('raw', 'invoices') }}
