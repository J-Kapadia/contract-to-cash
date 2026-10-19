{{ config(materialized='view') }}

select
    rate_on_booking_bps::bigint as rate_on_booking_bps,
    rate_on_collection_bps::bigint as rate_on_collection_bps,
    effective_from::date as effective_from,
    known_on::date as known_on,
    _month,
    _row_hash
from {{ source('raw', 'commission_plan') }}
