{#-
  Expected cash in per case and day over the 91 days after the as-of date, as cents, by line:

  - open_ar: the open invoices of the closed month of the as-of date, as `fct_expected_cash` dates
    them (base, downside and upside; an at-risk invoice keeps that mart's cents).
  - contracted, renewal, expansion, new: the invoices of `fc_billing_schedule` dated after the
    as-of date, each dated by `expected_pay_date()` from its invoice date plus the case's days to
    pay of its customer in `fct_days_to_pay` for that month: base the median, downside the 80th
    percentile, upside the median again (a forecast invoice has no terms to cap it). A customer
    with no row there takes the pooled row (customer_id '*'); with no pooled row either, the date
    is the invoice date. An invoice dated on or before the as-of date is already in the books and
    is not read here.

  A day with no cash has no row. Cash expected after the horizon is left out.
-#}
{%- set as_of = "'" ~ var('as_of') ~ "'::date" -%}
{%- set month = "strftime(" ~ as_of ~ ", '%Y-%m')" -%}

with
horizon as (
    select {{ as_of }} + 1 as first_day, {{ as_of }} + 91 as last_day
),

open_ar as (
    select "case", expected_date as date, 'open_ar' as line, cents
    from {{ ref('fct_expected_cash') }}
    where month = {{ month }} and cents <> 0
),

own_dtp as (
    select customer_id, median_days, p80_days
    from {{ ref('fct_days_to_pay') }}
    where month = {{ month }}
),

invoices as (
    select s.source as line, s."case", s.customer_id, s.invoice_date, s.amount_cents
    from {{ ref('fc_billing_schedule') }} s
    where s.invoice_date > {{ as_of }}
),

dated as (
    select
        i.line,
        i."case",
        i.amount_cents,
        {{ expected_pay_date(
            'i.invoice_date', 'i.invoice_date', as_of,
            "case i.\"case\" when 'downside' then coalesce(own.p80_days, pooled.p80_days) else coalesce(own.median_days, pooled.median_days) end",
            'null', source('raw', 'ref_business_days')) }} as date
    from invoices i
    left join own_dtp own on own.customer_id = i.customer_id
    left join own_dtp pooled on pooled.customer_id = '*'
),

forecast_invoices as (
    select "case", date, line, amount_cents as cents from dated
),

all_lines as (
    select * from open_ar
    union all
    select * from forecast_invoices
)

select
    {{ as_of }} as as_of,
    a."case",
    a.date,
    a.line,
    sum(a.cents)::bigint as cents
from all_lines a
cross join horizon h
where a.date between h.first_day and h.last_day
group by a."case", a.date, a.line
having sum(a.cents) <> 0
