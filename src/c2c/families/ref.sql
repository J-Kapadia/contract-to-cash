-- Each row of the reference files: a leaf over the row itself, keyed by the file and the row's key.
select
    'ref/' || file || '/' || key as number_id,
    null::varchar as month,
    null::bigint as value_cents,
    min(value) as value_text,
    'text' as unit,
    'leaf' as kind,
    'select _row_hash from raw.ref_' || file || ' where ' || column_name || ' = ''' || replace(key, '''', '''''') || ''''
        as source_query,
    null::varchar[] as children,
    'ref_' || file as step
from (
    select 'business_days' as file, 'date' as column_name, date as key, is_business_day as value from raw.ref_business_days
    union all select 'customer_matches', 'concat_ws(''|'', a, b)', concat_ws('|', a, b), decision from raw.ref_customer_matches
    union all select 'freemail_domains', 'domain', domain, domain from raw.ref_freemail_domains
    union all select 'gl_accounts', 'gl_account', gl_account, name from raw.ref_gl_accounts
    union all select 'legal_suffixes', 'suffix', suffix, suffix from raw.ref_legal_suffixes
)
group by file, column_name, key
order by number_id
