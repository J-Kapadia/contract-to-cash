{#-
  Forecast contracts for the open opportunities of the latest pipeline snapshot on or before the
  as-of date: per case, one row per open new or expansion opportunity at its annual contract value
  times the case's win rate for its stage, starting on its expected close date and running for its
  term with its billing frequency. The rates are the case-adjusted `rate_win_<stage>` inputs (the
  upside's capped at 100%, the downside's halved), so `annual_fee_cents` is `acv * weight // 10000`;
  the case's `pipeline_shift_days` moves every start date out.

  A stale opportunity (open, with an expected close date before the as-of date) starts at the as-of
  date plus the policy's `stale_opp_shift_days` instead, at its weight times `stale_opp_weight_bps`,
  and carries `stale`. Opportunities of type renewal are not read: the expiring contract already
  carries the renewal. Where the books are silent, the case shift is added to a stale start too, the
  weight is floored to whole basis points before the fee is taken, and a row is kept when its weight
  is zero.
-#}
{%- set as_of = "'" ~ var('as_of') ~ "'::date" -%}

with
latest as (
    select max(snapshot_date::date) as snapshot_date
    from {{ ref('pit_pipeline') }}
    where snapshot_date::date <= {{ as_of }}
),

open_opps as (
    select
        p.opp_id,
        p.crm_account_id as customer_id,
        p.opp_type as source,
        p.stage,
        p.acv_cents::bigint as acv_cents,
        p.term_months::int as term_months,
        p.billing_frequency,
        p.expected_close_date::date as expected_close_date,
        p.expected_close_date::date < {{ as_of }} as stale
    from {{ ref('pit_pipeline') }} p
    inner join latest l on l.snapshot_date = p.snapshot_date::date
    where p.stage in ('S1', 'S2', 'S3', 'S4', 'S5')
      and p.opp_type in ('new', 'expansion')
),

win_rates as (
    select "case", regexp_extract(driver, '^rate_win_(S[1-5])$', 1) as stage, value as rate_bps
    from fc_build.inputs
    where as_of = {{ as_of }} and regexp_matches(driver, '^rate_win_S[1-5]$')
),

shifts as (
    select "case", value as shift_days
    from fc_build.inputs
    where as_of = {{ as_of }} and driver = 'pipeline_shift_days'
),

weighted as (
    select
        o.*,
        w."case",
        s.shift_days,
        case when o.stale then w.rate_bps * {{ policy('stale_opp_weight_bps') }} // 10000 else w.rate_bps end as weight_bps,
        case
            when o.stale then {{ as_of }} + {{ policy('stale_opp_shift_days') }}::int
            else o.expected_close_date
        end as start_date
    from open_opps o
    inner join win_rates w on w.stage = o.stage
    inner join shifts s on s."case" = w."case"
)

select
    {{ as_of }} as as_of,
    "case",
    source,
    customer_id,
    opp_id,
    (start_date + shift_days::int)::date as term_start,
    ((start_date + shift_days::int)::date + to_months(term_months))::date - 1 as term_end,
    billing_frequency,
    acv_cents * weight_bps // 10000 as annual_fee_cents,
    stale
from weighted
