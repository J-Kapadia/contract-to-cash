{{ config(materialized='view') }}

select
    commitment_id,
    vendor_name,
    category,
    due_date::date as due_date,
    amount_cents::bigint as amount_cents,
    known_on::date as known_on,
    _month,
    _row_hash
from {{ source('raw', 'vendor_commitments') }}
