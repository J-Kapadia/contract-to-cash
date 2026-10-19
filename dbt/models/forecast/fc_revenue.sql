{#-
  Forecast recognised revenue per case, month and source for the 12 months after the as-of date,
  from `fc_billing_schedule` through `ratable_cents()`. A source with no revenue in a month has no
  row.
-#}
{%- set as_of = "'" ~ var('as_of') ~ "'::date" -%}

with months as (
    select strftime((date_trunc('month', {{ as_of }}) + to_months(i))::date, '%Y-%m') as month
    from range(1, 13) as t(i)
)

select
    {{ as_of }} as as_of,
    schedule."case",
    months.month,
    schedule.source,
    sum({{ ratable_cents('schedule.amount_cents', 'schedule.service_start', 'schedule.service_end', 'months.month') }})::bigint
        as recognised_cents
from {{ ref('fc_billing_schedule') }} as schedule
inner join months
    on months.month between strftime(schedule.service_start, '%Y-%m') and strftime(schedule.service_end, '%Y-%m')
group by all
having recognised_cents <> 0
order by "case", month, source
