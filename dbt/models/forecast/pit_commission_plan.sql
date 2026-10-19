{{ config(materialized='view') }}

select * replace (known_on::date as known_on)
from {{ source('raw', 'commission_plan') }}
where known_on::date <= '{{ var("as_of") }}'::date
