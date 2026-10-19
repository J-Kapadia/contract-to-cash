-- Recognised revenue per customer and line type: a leaf over the invoice lines recognised in the month.
-- Subscription and credit lines are recognised over their service period, an overage line in the month
-- its service starts; the customer of a line is the contract's, as in the model.
with revenue as (
    select
        customer_id,
        line_type,
        recognised_cents,
        '''' || replace(customer_id, '''', '''''') || '''' as q,
        '''' || replace(line_type, '''', '''''') || '''' as lt
    from mart.fct_revenue
    where month = $month and customer_id is not null
)

select
    'rev/' || $month || '/' || customer_id || '/' || line_type as number_id,
    $month as month,
    recognised_cents as value_cents,
    null::varchar as value_text,
    'cents' as unit,
    'leaf' as kind,
    'with customer_key as (select i.contract_id, coalesce(min(m.customer_id), min(k.crm_account_id)) as customer_id'
        || ' from int.stg_invoices as i join int.int_customer_members as m'
        || ' on m.source_system = ''billing'' and m.source_id = i.billing_customer_id'
        || ' left join int.stg_contracts as k on k.contract_id = i.contract_id group by i.contract_id)'
        || ' select i._row_hash from int.stg_invoices as i join customer_key using (contract_id)'
        || ' where customer_key.customer_id = ' || q || ' and i.line_type = ' || lt || ' and '
        || case line_type
            when 'overage' then 'strftime(i.service_start, ''%Y-%m'') = ''' || $month || ''''
            else '''' || $month || ''' between strftime(i.service_start, ''%Y-%m'') and strftime(i.service_end, ''%Y-%m'')'
        end as source_query,
    null::varchar[] as children,
    'fct_revenue' as step
from revenue
order by number_id
