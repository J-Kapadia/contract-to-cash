{{ config(materialized='view') }}

select
    pay_date::date as pay_date,
    half,
    cash_cents::bigint as cash_cents,
    headcount::bigint as headcount,
    known_on::date as known_on,
    _month,
    _row_hash
from {{ source('raw', 'payroll_runs') }}
