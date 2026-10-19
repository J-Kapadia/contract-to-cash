-- Every other row of the rollup table for the month, the totals, counts and rates the memo prints:
-- `rollup/<month>/<metric>`, the value carried as stored (a row without enough history carries
-- `insufficient_history` in value_text and no value_cents). The stale-opportunity count is a leaf over
-- the month-end pipeline snapshot it counts; any other row is a leaf over the row itself, hashed the way
-- the loader hashes a source row.
select
    'rollup/' || month || '/' || metric as number_id,
    month,
    value_cents,
    value_text,
    case when metric like '%\_bps' escape '\' then 'bps' when value_cents is null then 'text' else 'count' end as unit,
    'leaf' as kind,
    case when metric = 'stale_opps'
        then 'select _row_hash from raw.pipeline_snapshots where _month = ''' || month || ''''
        else 'select sha256(concat_ws(chr(31), month, metric, coalesce(value_cents::varchar, ''''), coalesce(value_text, '''')))'
            || ' as _row_hash from mart.fct_rollup where month = ''' || month || ''' and metric = ''' || replace(metric, '''', '''''') || ''''
    end as source_query,
    null::varchar[] as children,
    'fct_rollup' as step
from mart.fct_rollup
where month = $month and not starts_with(metric, 'master/')
order by number_id
