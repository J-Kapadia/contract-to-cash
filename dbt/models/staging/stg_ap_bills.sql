{{ config(materialized='view') }}

select
    bill_id,
    vendor_name,
    category,
    bill_date::date as bill_date,
    due_date::date as due_date,
    amount_cents::bigint as amount_cents,
    nullif(paid_bank_txn_id, '') as paid_bank_txn_id,
    known_on::date as known_on,
    _month,
    _row_hash
from {{ source('raw', 'ap_bills') }}
