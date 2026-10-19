-- One provenance record per measured rate in fc_build.fc_rates: the number id, the month of the
-- as-of date, a text naming the rate and the fallback it used, and the query that selects the
-- books' rows the rate was measured from. The rows are exactly those the rate's model reads: the
-- contracts (and their lines) of the cohort, or of both cohorts when a fallback applied, and the
-- pipeline snapshots of the stage and the won snapshots of the same opportunities.
-- Parameters: $cohort_window_months, $stage_win_window_months.
with
templates as (
    select
        $$with expiring as (
    select crm_account_id, renewal_number::int as renewal_number from raw.contracts
    where known_on::date <= '@as_of@'::date and term_end::date <= '@as_of@'::date
      and term_end::date > ('@as_of@'::date - to_months(@cohort_window@))::date and (@cohort_filter@)
), rows as (
    select c._row_hash, c.contract_id, c.version from raw.contracts c
    where c.known_on::date <= '@as_of@'::date and exists (
        select 1 from expiring e where e.crm_account_id = c.crm_account_id
        and c.renewal_number::int in (e.renewal_number, e.renewal_number + 1))
)
select _row_hash from rows
union all select l._row_hash from raw.contract_lines l join rows using (contract_id, version)
where l.known_on::date <= '@as_of@'::date$$ as contract_rows,
        $$with seen as (
    select distinct opp_id from raw.pipeline_snapshots
    where known_on::date <= '@as_of@'::date and opp_type <> 'renewal' and stage = '@stage@'
      and snapshot_date::date > ('@as_of@'::date - to_months(@stage_window@))::date
)
select _row_hash from raw.pipeline_snapshots
where known_on::date <= '@as_of@'::date and opp_type <> 'renewal' and opp_id in (select opp_id from seen)
  and (stage in ('@stage@', 'won'))$$ as pipeline_rows
)
select
    'rate/' || r.as_of::varchar || '/' || r.kind || '/' || r.cohort_or_stage as number_id,
    strftime(r.as_of, '%Y-%m') as month,
    r.rate_bps::varchar || ' bps, fallback ' || r.fallback as value_text,
    replace(replace(replace(replace(replace(
        case when r.kind = 'win' then t.pipeline_rows else t.contract_rows end,
        '@as_of@', r.as_of::varchar),
        '@cohort_window@', $cohort_window_months::varchar),
        '@stage_window@', $stage_win_window_months::varchar),
        '@stage@', r.cohort_or_stage),
        '@cohort_filter@', case when r.fallback <> 'none' then 'true'
                                when r.cohort_or_stage = 'first' then 'renewal_number::int = 0'
                                else 'renewal_number::int <> 0' end) as source_query
from fc_build.fc_rates r cross join templates t
order by number_id
