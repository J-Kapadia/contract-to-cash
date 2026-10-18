select
    payment_id,
    bank_txn_id,
    billing_customer_id,
    invoice_id,
    applied_cents::bigint as applied_cents,
    applied_on::date as applied_on,
    known_on::date as known_on,
    _month,
    _row_hash
from {{ source('raw', 'payments') }}
