-- The one-time items of the month: the total of each category in cents (credit memos, write-offs, manual
-- adjustments), a leaf over the category's raw rows of the month, and each item in cents, a leaf over the
-- item's own raw rows: an invoice line, a ledger line, or all the lines of a ledger entry.
with items as (
    select
        category,
        ref_id,
        amount_cents,
        split_part(ref_id, '/', 1) as head,
        split_part(ref_id, '/', 2) as line_no
    from mart.fct_onetime_items
    where month = $month
),

quoted as (
    select
        category,
        ref_id,
        amount_cents,
        '''' || replace(head, '''', '''''') || '''' as q_head,
        line_no
    from items
),

leaves as (
    select
        'onetime/' || $month || '/credit_memo/' || ref_id as number_id,
        amount_cents as value_cents,
        'select _row_hash from raw.invoices where invoice_id = ' || q_head || ' and line_no = ' || line_no
            as source_query
    from quoted
    where category = 'credit_memo'
    union all
    select
        'onetime/' || $month || '/write_off/' || ref_id,
        amount_cents,
        'select _row_hash from raw.gl where entry_id = ' || q_head || ' and line_no = ' || line_no
    from quoted
    where category = 'write_off'
    union all
    select
        'onetime/' || $month || '/manual_adjustment/' || ref_id,
        amount_cents,
        'select _row_hash from raw.gl where entry_id = ' || q_head
    from quoted
    where category = 'manual_adjustment'
),

totals as (
    select
        'onetime/' || $month || '/' || name as number_id,
        coalesce(sum(items.amount_cents), 0)::bigint as value_cents,
        case
            when count(items.ref_id) = 0 then
                'select _row_hash from raw.' || case when category = 'credit_memo' then 'invoices' else 'gl' end
                    || ' where false'
            when category = 'credit_memo' then
                'select _row_hash from raw.invoices where (invoice_id, line_no) in ('
                    || string_agg('(' || quoted.q_head || ', ' || quoted.line_no || ')', ', ') || ')'
            when category = 'write_off' then
                'select _row_hash from raw.gl where (entry_id, line_no) in ('
                    || string_agg('(' || quoted.q_head || ', ' || quoted.line_no || ')', ', ') || ')'
            else
                'select _row_hash from raw.gl where entry_id in (' || string_agg(quoted.q_head, ', ') || ')'
        end as source_query
    from (
        values ('credit_memo', 'credit_memos'), ('write_off', 'write_offs'), ('manual_adjustment', 'manual_adjustments')
    ) as categories (category, name)
    left join items using (category)
    left join quoted using (category, ref_id)
    group by category, name
)

select number_id, $month as month, value_cents, null::varchar as value_text, 'cents' as unit, 'leaf' as kind,
    source_query, null::varchar[] as children, 'fct_onetime_items' as step
from leaves
union all
select number_id, $month as month, value_cents, null::varchar as value_text, 'cents' as unit, 'leaf' as kind,
    source_query, null::varchar[] as children, 'fct_onetime_items' as step
from totals
order by number_id
