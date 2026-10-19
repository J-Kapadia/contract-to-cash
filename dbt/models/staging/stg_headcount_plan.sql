{{ config(materialized='view') }}

select
    month,
    (month || '-01')::date as month_start,
    delta_cash_per_run_cents::bigint as delta_cash_per_run_cents,
    known_on::date as known_on,
    _month,
    _row_hash
from {{ source('raw', 'headcount_plan') }}
