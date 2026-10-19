-- Each policy key: a leaf over the key's row of the signed configuration. The policy table has no row
-- hash of its own, so the leaf hashes the key and value the way the loader hashes a row.
select
    'cfg/' || key as number_id,
    null::varchar as month,
    min(value)::bigint as value_cents,
    null::varchar as value_text,
    'policy' as unit,
    'leaf' as kind,
    'select sha256(concat_ws(chr(31), key, value)) as _row_hash from raw.ref_policy where key = ''' || replace(key, '''', '''''') || '''' as source_query,
    null::varchar[] as children,
    'ref_policy' as step
from raw.ref_policy
group by key
order by number_id
