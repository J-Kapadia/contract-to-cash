-- The month's walk from recognised revenue to cash receipts:
--   revenue + deferred revenue movement = net billings
--   net billings - receivables movement = receipts
-- Each movement is closing minus opening, summed over customers. Net billings are therefore the
-- signed sum of the month's invoice lines (a credit line is a negative billing) and receipts are
-- derived, never plugged; assert_bridge_conserves checks both against their own sources. One row
-- per month and line, for every month the deferred revenue roll-forward covers.
--
-- Where the plan is silent: the months are those of the deferred revenue roll-forward. The cash
-- roll-forward is not read here; the hint below only makes it build first, because the conservation
-- test compares the receipts line with it.
-- depends_on: {{ ref('fct_cash_rollforward') }}

with months as (
    select distinct month from {{ ref('fct_defrev_rollforward') }}
),

revenue as (
    select month, sum(recognised_cents) as cents
    from {{ ref('fct_revenue') }}
    group by month
),

defrev as (
    select month, sum(closing_cents - opening_cents) as cents
    from {{ ref('fct_defrev_rollforward') }}
    group by month
),

ar as (
    select month, sum(closing_cents - opening_cents) as cents
    from {{ ref('fct_ar_rollforward') }}
    group by month
),

walk as (
    select
        months.month,
        coalesce(revenue.cents, 0) as revenue,
        coalesce(defrev.cents, 0) as defrev_movement,
        coalesce(revenue.cents, 0) + coalesce(defrev.cents, 0) as net_billings,
        coalesce(ar.cents, 0) as ar_movement
    from months
    left join revenue using (month)
    left join defrev using (month)
    left join ar using (month)
),

lines as (
    select month, 'revenue' as line, revenue as cents from walk
    union all select month, 'defrev_movement', defrev_movement from walk
    union all select month, 'net_billings', net_billings from walk
    union all select month, 'ar_movement', ar_movement from walk
    union all select month, 'receipts', net_billings - ar_movement from walk
)

select month, line, cents::bigint as cents
from lines
order by month, case line
    when 'revenue' then 1
    when 'defrev_movement' then 2
    when 'net_billings' then 3
    when 'ar_movement' then 4
    else 5
end
