-- remediation: The columns of {file} differ from the {baseline_month} close: now {columns}, then {previous_columns}; restore the file's layout, or close {baseline_month} again on the new layout so it becomes the baseline.
-- stage: raw
-- Each books file has the columns it had when the latest earlier month was closed. The layout of every
-- file is recorded as a number at each close; a file that appears or vanishes shows `none` on its
-- missing side. A baseline closed before layouts were recorded has nothing to compare with, so the
-- control is silent then.

with baseline as (
    select max(month) as month
    from state.close_state
    where status in ('closed', 'locked') and month < $month
),

previous as (
    select
        split_part(records.number_id, '/', 3) as stem,
        records.value_text as previous_columns,
        baseline.month as baseline_month
    from prov.records as records
    inner join baseline on starts_with(records.number_id, 'schema/' || baseline.month || '/')
),

current as (
    select
        table_name as stem,
        string_agg(column_name, ',' order by ordinal_position) as current_columns
    from information_schema.columns
    where table_schema = 'raw'
        and not starts_with(table_name, 'ref_')
        and column_name not in ('_row_hash', '_month', '_load_id')
    group by table_name
)

select
    coalesce(current.stem, previous.stem) || '.csv' as file,
    (select month from baseline) as baseline_month,
    coalesce(current.current_columns, 'none') as "columns",
    coalesce(previous.previous_columns, 'none') as previous_columns
from current
full outer join previous on previous.stem = current.stem
where exists (select 1 from previous)
    and previous.previous_columns is distinct from current.current_columns
order by file
