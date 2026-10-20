-- The layout of each books file as loaded: its columns in file order, one text number per file. The raw
-- tables are rebuilt at every close, so this record is the only memory of a file's layout between two
-- closes. A leaf over the layout itself, so its source query is a constant over the stem and the columns.
with layouts as (
    select
        table_name as stem,
        string_agg(column_name, ',' order by ordinal_position) as columns
    from information_schema.columns
    where table_schema = 'raw'
        and not starts_with(table_name, 'ref_')
        and column_name not in ('_row_hash', '_month', '_load_id')
    group by table_name
)

select
    'schema/' || $month || '/' || stem as number_id,
    $month as month,
    null::bigint as value_cents,
    columns as value_text,
    'text' as unit,
    'leaf' as kind,
    'select sha256(''' || replace(stem || ':' || columns, '''', '''''') || ''') as _row_hash' as source_query,
    null::varchar[] as children,
    'raw' as step
from layouts
order by number_id
