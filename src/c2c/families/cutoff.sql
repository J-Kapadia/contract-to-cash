-- Invoices whose ledger entries sit in another period than the month of their invoice date: the count of
-- findings up to the month, and each finding (invoice and period) in cents, a leaf over the invoice's
-- misplaced ledger lines. A finding of an earlier month is registered again under every later month.
with findings as (
    select
        'cutoff/' || $month || '/' || invoice_id || '/' || gl_period as number_id,
        amount_cents as value_cents,
        'select _row_hash from raw.gl where source_ref = ''' || replace(invoice_id, '''', '''''')
            || ''' and gl_period = ''' || replace(gl_period, '''', '''''') || '''' as source_query
    from mart.fct_cutoff_findings
    where invoice_month <= $month
),

total as (
    select
        'cutoff/' || $month || '/findings' as number_id,
        count(*)::bigint as value_cents,
        case when count(*) = 0 then 'select _row_hash from raw.gl where false' else
            'select gl._row_hash from raw.gl as gl join (select invoice_id,'
                || ' strftime(min(invoice_date), ''%Y-%m'') as invoice_month from int.stg_invoices group by invoice_id)'
                || ' as inv on inv.invoice_id = gl.source_ref where gl.gl_period <> inv.invoice_month'
                || ' and inv.invoice_month <= ''' || $month || ''''
        end as source_query
    from findings
)

select number_id, $month as month, value_cents, null::varchar as value_text, 'cents' as unit, 'leaf' as kind,
    source_query, null::varchar[] as children, 'fct_cutoff_findings' as step
from findings
union all
select number_id, $month as month, value_cents, null::varchar as value_text, 'count' as unit, 'leaf' as kind,
    source_query, null::varchar[] as children, 'fct_cutoff_findings' as step
from total
order by number_id
