{#-
  Cash out per case and day over the 91 days after the as-of date, as positive cents, only on the
  days something is paid:

  - payroll: on the 15th and the last day of each month, moved to the previous business day. A run
    is the last actual run of the same half-month plus the per-run deltas of the headcount plan
    that were known by the as-of date and start after that run's month, up to the run's month.
  - cloud: bills not yet paid at the as-of date are paid on their due date; each month after the
    latest billed one is accrued at the mean of the trailing billed months (plus the case's
    basis points) and paid 30 days after that month's end.
  - vendors, events and rent: bills not yet paid at the as-of date on their due date, and the
    commitments due in the horizon that no known bill covers (same vendor, category and due date).
    A bill already past due and unpaid is paid on the first day of the horizon.

  Everything is read as known on the as-of date. Commissions are not part of this model.
-#}
{%- set as_of = "'" ~ var('as_of') ~ "'::date" -%}

with
horizon as (
    select {{ as_of }} + 1 as first_day, {{ as_of }} + 91 as last_day
),

cases as (
    select
        "case",
        max(value) filter (where driver = 'cloud_up_bps') as cloud_up_bps,
        max(value) filter (where driver = 'cloud_trailing_months') as cloud_trailing_months
    from fc_build.inputs
    where as_of = {{ as_of }}
    group by "case"
),

business_days as (
    select "date"::date as day
    from {{ source('raw', 'ref_business_days') }}
    where is_business_day::int = 1
),

-- Payroll

months as (
    select (date_trunc('month', {{ as_of }}) + to_months(i))::date as month_start
    from range(0, 5) as t(i)
),

nominal_runs as (
    select month_start, 'mid' as half, month_start + 14 as nominal_date from months
    union all
    select month_start, 'end', last_day(month_start) from months
),

scheduled_runs as (
    select
        n.month_start,
        n.half,
        (select max(b.day) from business_days b where b.day <= n.nominal_date) as pay_date
    from nominal_runs n
),

last_actual_run as (
    select half, cash_cents, date_trunc('month', pay_date)::date as run_month
    from {{ ref('stg_payroll_runs') }}
    where known_on <= {{ as_of }} and pay_date <= {{ as_of }}
    qualify row_number() over (partition by half order by pay_date desc) = 1
),

payroll as (
    select
        s.pay_date,
        'payroll' as line,
        l.cash_cents + coalesce((
            select sum(h.delta_cash_per_run_cents)
            from {{ ref('stg_headcount_plan') }} h
            where h.known_on <= {{ as_of }}
              and h.month_start > l.run_month
              and h.month_start <= s.month_start
        ), 0) as cents
    from scheduled_runs s
    join last_actual_run l using (half)
),

-- Cloud

billed_cloud_months as (
    select date_trunc('month', bill_date)::date as month_start, sum(amount_cents) as cents
    from {{ ref('stg_ap_bills') }}
    where category = 'cloud' and known_on <= {{ as_of }}
    group by 1
),

ranked_cloud_months as (
    select *, row_number() over (order by month_start desc) as recency from billed_cloud_months
),

accrual as (
    select
        c."case",
        (sum(m.cents) // count(*)) * (10000 + c.cloud_up_bps) // 10000 as cents
    from cases c
    join ranked_cloud_months m on m.recency <= c.cloud_trailing_months
    group by c."case", c.cloud_up_bps
),

unbilled_cloud_months as (
    select (m.month_start + to_months(i))::date as month_start
    from (select max(month_start) as month_start from billed_cloud_months) m, range(1, 7) as t(i)
),

forecast_cloud as (
    select a."case", last_day(u.month_start) + 30 as pay_date, 'cloud' as line, a.cents
    from unbilled_cloud_months u
    cross join accrual a
),

-- Open bills and commitments

open_bills as (
    select b.*
    from {{ ref('stg_ap_bills') }} b
    where b.known_on <= {{ as_of }}
      and not exists (
          select 1
          from {{ ref('pit_bank') }} k
          where k.bank_txn_id = b.paid_bank_txn_id and k.posted_on::date <= {{ as_of }})
),

open_commitments as (
    select c.*
    from {{ ref('stg_vendor_commitments') }} c
    where c.known_on <= {{ as_of }}
      and not exists (
          select 1
          from {{ ref('stg_ap_bills') }} b
          where b.known_on <= {{ as_of }}
            and b.vendor_name = c.vendor_name
            and b.category = c.category
            and b.due_date = c.due_date)
),

payables as (
    select greatest(due_date, {{ as_of }} + 1) as pay_date, category, amount_cents from open_bills
    union all
    select due_date, category, amount_cents from open_commitments where due_date > {{ as_of }}
),

lines as (
    select
        pay_date,
        case category when 'cloud' then 'cloud' when 'events' then 'events' when 'rent' then 'rent' else 'vendors' end as line,
        amount_cents as cents
    from payables
),

per_case as (
    select c."case", p.pay_date, p.line, p.cents
    from cases c
    cross join (
        select pay_date, line, cents from payroll
        union all
        select pay_date, line, cents from lines
    ) p
    union all
    select "case", pay_date, line, cents from forecast_cloud
)

select
    {{ as_of }} as as_of,
    p."case",
    p.pay_date as date,
    p.line,
    sum(p.cents)::bigint as cents
from per_case p
cross join horizon h
where p.pay_date between h.first_day and h.last_day
group by p."case", p.pay_date, p.line
