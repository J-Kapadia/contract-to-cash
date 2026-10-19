{#-
  Forecast contracts for the subscriptions that expire within 12 months after the as-of date: per
  case, each expiring subscription line yields a `renewal` row at its fee times the case's renewal
  rate for the contract's cohort, and an `expansion` row at its fee times the cohort's expansion
  rate, both starting the day after the term ends with the contract's billing frequency and
  payment terms. `first` is a contract's first renewal, `later` every other; the rates are the
  case-adjusted `rate_<kind>_<cohort>` inputs, so `annual_fee_cents` is `fee * rate_bps // 10000`.

  Opportunities of type renewal are not read: the expiring contract already carries the renewal.
  A contract that ended before the as-of date is not renewed here, nor is one whose account already
  holds the next renewal number: that contract is part of the contracted schedule. Where the books
  are silent, a forecast term is 12 months, the line and contract ids are the expiring ones with
  the source appended, and a row is kept when its rate is zero, so each line has a row per case
  and source.
-#}
{%- set as_of = "'" ~ var('as_of') ~ "'::date" -%}

with
latest as (
    select * from {{ ref('pit_contracts') }}
    qualify row_number() over (partition by contract_id order by version::int desc) = 1
),

expiring as (
    select
        c.contract_id,
        c.version,
        c.crm_account_id as customer_id,
        c.term_end::date as term_end,
        c.billing_frequency,
        c.payment_terms_days::int as payment_terms_days,
        case when c.renewal_number::int = 0 then 'first' else 'later' end as cohort
    from latest c
    where c.term_end::date >= {{ as_of }}
      and c.term_end::date <= ({{ as_of }} + to_months(12))::date
      and not exists (
          select 1 from latest next
          where next.crm_account_id = c.crm_account_id
            and next.renewal_number::int = c.renewal_number::int + 1
      )
),

lines as (
    select e.*, l.line_id, l.annual_fee_cents::bigint as fee_cents
    from expiring e
    inner join {{ ref('pit_contract_lines') }} l
        on l.contract_id = e.contract_id and l.version = e.version
    where l.product = 'subscription'
),

rates as (
    select
        "case",
        regexp_extract(driver, '^rate_(renewal|expansion)_(first|later)$', 1) as source,
        regexp_extract(driver, '^rate_(renewal|expansion)_(first|later)$', 2) as cohort,
        value as rate_bps
    from fc_build.inputs
    where as_of = {{ as_of }}
      and regexp_matches(driver, '^rate_(renewal|expansion)_(first|later)$')
)

select
    {{ as_of }} as as_of,
    r."case",
    r.source,
    l.customer_id,
    l.contract_id || ':' || r.source as contract_id,
    l.line_id || ':' || r.source as line_id,
    l.term_end + 1 as term_start,
    (l.term_end + 1 + to_months(12))::date - 1 as term_end,
    l.billing_frequency,
    l.payment_terms_days,
    l.fee_cents * r.rate_bps // 10000 as annual_fee_cents
from lines l
inner join rates r on r.cohort = l.cohort
