{#-
  Rates measured from the books known on the as-of date, in integer basis points:

  - renewal and expansion, per cohort: `first` (a contract's first renewal) and `later`, weighted by
    annual subscription fees over the contracts that expired in the trailing cohort window. An
    expiring contract counts as renewed when its account holds a contract with the next renewal
    number. Renewal keeps min(expiring, renewed) fees, expansion the excess of renewed over expiring.
  - win, per stage S1..S5: the share of opportunities seen in the stage during the trailing stage
    window that a later snapshot shows as won, counted by opportunity; renewal opportunities are left
    out because the renewal contract carries them.

  `numerator * 10000 // denominator`. A cohort with fewer than `min_observations` expiries takes the
  pooled rate over both cohorts; when that is also too thin, or a stage is, the policy default
  (a stage's own default; none for renewal and expansion, so 0 bps).
-#}
{%- set as_of = "'" ~ var('as_of') ~ "'::date" -%}
{%- set min_obs = var('min_observations') -%}

with
latest as (
    select * from {{ ref('pit_contracts') }}
    qualify row_number() over (partition by contract_id order by version::int desc) = 1
),

fees as (
    select contract_id, version, sum(annual_fee_cents::bigint) as arr
    from {{ ref('pit_contract_lines') }}
    where product = 'subscription'
    group by contract_id, version
),

contracts as (
    select
        c.crm_account_id,
        c.renewal_number::int as renewal_number,
        c.term_end::date as term_end,
        coalesce(f.arr, 0) as arr
    from latest c
    left join fees f on f.contract_id = c.contract_id and f.version = c.version
),

renewals as (
    select crm_account_id, renewal_number, max(arr) as arr
    from contracts
    group by crm_account_id, renewal_number
),

expiries as (
    select
        case when c.renewal_number = 0 then 'first' else 'later' end as cohort,
        c.arr,
        coalesce(r.arr, 0) as renewed_arr
    from contracts c
    left join renewals r
        on r.crm_account_id = c.crm_account_id and r.renewal_number = c.renewal_number + 1
    where c.term_end <= {{ as_of }}
      and c.term_end > ({{ as_of }} - to_months({{ var('cohort_window_months') }}))::date
      and c.arr > 0
),

cohorts as (
    select
        g.cohort,
        count(e.cohort) as observations,
        coalesce(sum(e.arr), 0) as denominator,
        coalesce(sum(least(e.arr, e.renewed_arr)), 0) as renewal_numerator,
        coalesce(sum(greatest(e.renewed_arr - e.arr, 0)), 0) as expansion_numerator
    from (values ('first'), ('later')) as g(cohort)
    left join expiries e using (cohort)
    group by g.cohort
),

pooled as (
    select
        sum(observations) as observations,
        sum(denominator) as denominator,
        sum(renewal_numerator) as renewal_numerator,
        sum(expansion_numerator) as expansion_numerator
    from cohorts
),

snapshots as (
    select opp_id, stage, snapshot_date::date as seen_on
    from {{ ref('pit_pipeline') }}
    where opp_type <> 'renewal'
),

won as (
    select opp_id, min(seen_on) as won_on from snapshots where stage = 'won' group by opp_id
),

seen as (
    select opp_id, stage, min(seen_on) as first_seen
    from snapshots
    where seen_on > ({{ as_of }} - to_months({{ var('stage_win_window_months') }}))::date
    group by opp_id, stage
),

stages as (
    select
        g.stage,
        g.default_bps,
        count(s.stage) as observations,
        count(s.stage) filter (where w.won_on > s.first_seen) as numerator
    from (values
        ('S1', {{ var('stage_default_s1_bps') }}), ('S2', {{ var('stage_default_s2_bps') }}),
        ('S3', {{ var('stage_default_s3_bps') }}), ('S4', {{ var('stage_default_s4_bps') }}),
        ('S5', {{ var('stage_default_s5_bps') }})
    ) as g(stage, default_bps)
    left join seen s using (stage)
    left join won w on w.opp_id = s.opp_id
    group by g.stage, g.default_bps
),

measured as (
    select 'renewal' as kind, c.cohort as cohort_or_stage, c.observations, c.denominator,
           c.renewal_numerator as numerator, p.observations as pooled_observations,
           p.denominator as pooled_denominator, p.renewal_numerator as pooled_numerator, 0 as default_bps
    from cohorts c cross join pooled p
    union all
    select 'expansion', c.cohort, c.observations, c.denominator, c.expansion_numerator,
           p.observations, p.denominator, p.expansion_numerator, 0
    from cohorts c cross join pooled p
    union all
    select 'win', stage, observations, observations, numerator, 0, 0, 0, default_bps
    from stages
),

chosen as (
    select
        *,
        case
            when observations >= {{ min_obs }} and denominator > 0 then 'none'
            when kind <> 'win' and pooled_observations >= {{ min_obs }} and pooled_denominator > 0 then 'pooled'
            else 'default'
        end as fallback
    from measured
)

select
    {{ as_of }} as as_of,
    kind,
    cohort_or_stage,
    (case fallback
        when 'none' then numerator * 10000 // denominator
        when 'pooled' then pooled_numerator * 10000 // pooled_denominator
        else default_bps
    end)::bigint as rate_bps,
    observations::int as observations,
    fallback
from chosen
