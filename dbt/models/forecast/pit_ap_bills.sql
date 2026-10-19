{{ config(materialized='view') }}

select * replace (known_on::date as known_on)
from {{ source('raw', 'ap_bills') }}
where known_on::date <= '{{ var("as_of") }}'::date
