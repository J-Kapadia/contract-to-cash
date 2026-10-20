-- Days sales outstanding per month: the month's closing receivables over all customers, times the
-- calendar days of the month, over the month's billings, in whole days (floor). A month with no
-- billings has no days outstanding.
with monthly as (
    select
        month,
        sum(closing_cents)::bigint as ar_closing_cents,
        sum(billed_cents)::bigint as billed_cents
    from {{ ref('fct_ar_rollforward') }}
    group by month
)

select
    month,
    ar_closing_cents,
    billed_cents,
    day(last_day(strptime(month || '-01', '%Y-%m-%d')))::bigint as days_in_month,
    case when billed_cents > 0 then (ar_closing_cents * days_in_month // billed_cents)::bigint end as dso_days -- single-definition: ok days outstanding, a ratio of cents to cents
from monthly
order by month
