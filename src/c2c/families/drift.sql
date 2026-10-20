-- What changed in the books this month: each first-seen ledger account, CRM account and contract line
-- type, and each source file whose row count moved against the previous month by at least the policy
-- threshold, a leaf over the rows it is about. The four counts (new accounts, customers, line types and
-- volume shifts) are always present, a leaf over the union of their items' rows; with no item they
-- stand over no row.
with months as (
    select
        $month as month,
        strftime(strptime($month || '-01', '%Y-%m-%d') - interval 1 month, '%Y-%m') as previous
),

items as (
    select
        'drift/' || d.month || '/' || d.category || '/' || d.key as number_id,
        d.month,
        d.category,
        d.key,
        case when d.category = 'volume_shift' then d.shift_bps else d.value_cents end as value_cents,
        case when d.category = 'volume_shift' then 'bps' else 'count' end as unit,
        case d.category
            when 'new_account' then
                'select _row_hash from raw.gl where _month = ''' || d.month || ''' and gl_account = '''
                    || replace(d.key, '''', '''''') || ''''
            when 'new_customer' then
                'select _row_hash from raw.crm_accounts where _month = ''' || d.month || ''' and crm_account_id = '''
                    || replace(d.key, '''', '''''') || ''''
            when 'new_line_type' then
                'select _row_hash from raw.contract_lines where _month = ''' || d.month || ''' and product = '''
                    || replace(d.key, '''', '''''') || ''''
            else
                'select _row_hash from raw.' || d.key || ' where _month in (''' || d.month || ''', '''
                    || months.previous || ''')'
        end as source_query
    from mart.fct_drift as d
    inner join months on months.month = d.month
),

categories as (
    select * from (values
        ('new_account', 'new_accounts'),
        ('new_customer', 'new_customers'),
        ('new_line_type', 'new_line_types'),
        ('volume_shift', 'volume_shifts')
    ) as t(category, metric)
),

totals as (
    select
        'drift/' || $month || '/' || categories.metric as number_id,
        $month as month,
        count(items.number_id)::bigint as value_cents,
        coalesce(string_agg(items.source_query, ' union all ' order by items.key),
            'select _row_hash from raw.gl where false') as source_query
    from categories
    left join items using (category)
    group by categories.metric
)

select number_id, month, value_cents, null::varchar as value_text, unit, 'leaf' as kind, source_query,
    null::varchar[] as children, 'fct_drift' as step
from items
union all
select number_id, month, value_cents, null::varchar as value_text, 'count' as unit, 'leaf' as kind, source_query,
    null::varchar[] as children, 'fct_drift' as step
from totals
order by number_id
