-- The size of each master customer: the member names (CRM account, billing customer, bank and ledger
-- names) merged under its id. A leaf over the raw rows of those members up to the month.
with customers as (
    select
        customer_id,
        member_count,
        '''' || replace(customer_id, '''', '''''') || '''' as q
    from mart.dim_customer
    where customer_id is not null
)

select
    'cust/' || $month || '/' || customer_id || '/members' as number_id,
    $month as month,
    member_count::bigint as value_cents,
    null::varchar as value_text,
    'count' as unit,
    'leaf' as kind,
    'select _row_hash from raw.crm_accounts where _month <= ''' || $month || ''' and crm_account_id in ('
        || 'select source_id from int.int_customer_members where source_system = ''crm'' and customer_id = ' || q || ')'
    || ' union all select _row_hash from raw.billing_customers where _month <= ''' || $month || ''' and billing_customer_id in ('
        || 'select source_id from int.int_customer_members where source_system = ''billing'' and customer_id = ' || q || ')'
    || ' union all select _row_hash from raw.bank_transactions where _month <= ''' || $month || ''' and counterparty_text in ('
        || 'select source_id from int.int_customer_members where source_system = ''bank_name'' and customer_id = ' || q || ')'
    || ' union all select _row_hash from raw.gl where _month <= ''' || $month || ''' and subledger_name in ('
        || 'select source_id from int.int_customer_members where source_system = ''gl_name'' and customer_id = ' || q || ')'
        as source_query,
    null::varchar[] as children,
    'dim_customer' as step
from customers
order by number_id
