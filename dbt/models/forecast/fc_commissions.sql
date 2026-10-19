{#-
  Commissions paid in the 91 days after the as-of date, per case and day, as positive cents:

  - on bookings: the commission plan's booking rate on the first-year fee (`annual_fee_cents`) of
    each forecast renewal, expansion and new contract, paid on the 15th of the month after the
    month its term starts;
  - on collections: the plan's collection rate on the cash in of the forecast contracts' invoices
    (the renewal, expansion and new lines of `fc_cash_in`), paid on the 15th of the month after the
    month it is collected.

  The rate is the plan row with the latest `effective_from` on or before the booking or collection
  date, among the rows known on the as-of date; with no such row the commission is zero. The 15th
  is not moved to a business day. Collections before the as-of date and bookings already in the
  books are not read here.

  A day with no commission has no row.
-#}
{%- set as_of = "'" ~ var('as_of') ~ "'::date" -%}

with
horizon as (
    select {{ as_of }} + 1 as first_day, {{ as_of }} + 91 as last_day
),

plan as (
    select rate_on_booking_bps, rate_on_collection_bps, effective_from
    from {{ ref('stg_commission_plan') }}
    where known_on <= {{ as_of }}
),

bookings as (
    select "case", term_start as event_date, annual_fee_cents as base_cents, 'booking' as kind
    from {{ ref('fc_contracts_renewal') }}
    union all
    select "case", term_start, annual_fee_cents, 'booking'
    from {{ ref('fc_contracts_pipeline') }}
),

collections as (
    select "case", date as event_date, cents as base_cents, 'collection' as kind
    from {{ ref('fc_cash_in') }}
    where line in ('renewal', 'expansion', 'new')
),

events as (
    select *, row_number() over () as event_id
    from (
        select * from bookings
        union all
        select * from collections
    )
),

rated as (
    select
        e."case",
        (date_trunc('month', e.event_date)::date + to_months(1) + to_days(14))::date as pay_date,
        e.base_cents * case e.kind when 'booking' then p.rate_on_booking_bps else p.rate_on_collection_bps end // 10000 as cents
    from events e
    inner join plan p on p.effective_from <= e.event_date
    qualify row_number() over (partition by e.event_id order by p.effective_from desc) = 1
)

select
    {{ as_of }} as as_of,
    r."case",
    r.pay_date as date,
    sum(r.cents)::bigint as cents
from rated r
cross join horizon h
where r.pay_date between h.first_day and h.last_day
group by r."case", r.pay_date
having sum(r.cents) <> 0
