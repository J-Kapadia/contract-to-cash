-- Deferred revenue per customer: opening, billings, recognised and closing, each a leaf over the
-- subscription and credit invoice lines it moves on. Billings are the lines invoiced in the month,
-- recognised the lines whose service period covers it; a balance is every line invoiced or started up
-- to the month it closes.
with months as (
    select
        $month as month,
        strftime(strptime($month, '%Y-%m') - interval 1 month, '%Y-%m') as previous
),

balances as (
    select
        fct_defrev_rollforward.customer_id,
        '''' || replace(fct_defrev_rollforward.customer_id, '''', '''''') || '''' as q,
        months.month,
        months.previous,
        fct_defrev_rollforward.opening_cents,
        fct_defrev_rollforward.additions_cents,
        fct_defrev_rollforward.reductions_cents,
        fct_defrev_rollforward.closing_cents
    from mart.fct_defrev_rollforward
    inner join months using (month)
    where fct_defrev_rollforward.customer_id is not null
),

metrics as (
    select
        customer_id, q, month, 'opening' as metric, opening_cents as cents,
        '(strftime(i.invoice_date, ''%Y-%m'') <= ''' || previous || ''' or strftime(i.service_start, ''%Y-%m'') <= '''
            || previous || ''')' as condition
    from balances
    union all
    select customer_id, q, month, 'billings', additions_cents,
        'strftime(i.invoice_date, ''%Y-%m'') = ''' || month || ''''
    from balances
    union all
    select customer_id, q, month, 'recognised', reductions_cents,
        '''' || month || ''' between strftime(i.service_start, ''%Y-%m'') and strftime(i.service_end, ''%Y-%m'')'
    from balances
    union all
    select customer_id, q, month, 'closing', closing_cents,
        '(strftime(i.invoice_date, ''%Y-%m'') <= ''' || month || ''' or strftime(i.service_start, ''%Y-%m'') <= '''
            || month || ''')'
    from balances
)

select
    'defrev/' || month || '/' || customer_id || '/' || metric as number_id,
    month,
    cents as value_cents,
    null::varchar as value_text,
    'cents' as unit,
    'leaf' as kind,
    'with customer_key as (select i.contract_id, coalesce(min(m.customer_id), min(k.crm_account_id)) as customer_id'
        || ' from int.stg_invoices as i join int.int_customer_members as m'
        || ' on m.source_system = ''billing'' and m.source_id = i.billing_customer_id'
        || ' left join int.stg_contracts as k on k.contract_id = i.contract_id group by i.contract_id)'
        || ' select i._row_hash from int.stg_invoices as i join customer_key using (contract_id)'
        || ' where customer_key.customer_id = ' || q || ' and i.line_type in (''subscription'', ''credit'') and '
        || condition as source_query,
    null::varchar[] as children,
    'fct_defrev_rollforward' as step
from metrics
order by number_id
