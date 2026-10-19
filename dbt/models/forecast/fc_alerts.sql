{#-
  The minimum-cash alert per case: the first week whose ending cash is below the policy's
  `min_cash_cents`, and by how much it falls short at the end of that week. A case that never
  falls below it has no row.
-#}
{%- set as_of = "'" ~ var('as_of') ~ "'::date" -%}

select
    {{ as_of }} as as_of,
    "case",
    week as first_breach_week,
    ({{ policy('min_cash_cents') }} - ending_cents)::bigint as shortfall_cents
from (select distinct as_of, "case", week, ending_cents from {{ ref('fc_cash_13w') }})
where ending_cents < {{ policy('min_cash_cents') }}
qualify row_number() over (partition by "case" order by week) = 1
