-- Customer concentration of the month's recognised revenue: the share held by the largest customer, the
-- five largest and the ten largest in basis points, each a leaf over every invoice line recognised in the
-- month; and each customer's revenue in cents and share in basis points, a leaf over that customer's lines.
-- The lines are selected as the revenue family selects them: a subscription or credit line over its
-- service period, an overage line in the month its service starts, the customer being the contract's.
with rows as (
    select
        customer_id,
        revenue_cents,
        share_bps,
        top1_bps,
        top5_bps,
        top10_bps,
        '''' || replace(customer_id, '''', '''''') || '''' as q
    from mart.fct_concentration
    where month = $month
),

lines as (
    select
        'with customer_key as (select i.contract_id, coalesce(min(m.customer_id), min(k.crm_account_id)) as customer_id'
            || ' from int.stg_invoices as i join int.int_customer_members as m'
            || ' on m.source_system = ''billing'' and m.source_id = i.billing_customer_id'
            || ' left join int.stg_contracts as k on k.contract_id = i.contract_id group by i.contract_id)'
            || ' select i._row_hash from int.stg_invoices as i join customer_key using (contract_id)'
            || ' where customer_key.customer_id is not null' as head,
        ' and ('
            || '(i.line_type = ''overage'' and strftime(i.service_start, ''%Y-%m'') = ''' || $month || ''')'
            || ' or (i.line_type <> ''overage'' and ''' || $month
            || ''' between strftime(i.service_start, ''%Y-%m'') and strftime(i.service_end, ''%Y-%m'')))' as recognised
),

leaves as (
    select 'top1' as metric, top1_bps as value, 'bps' as unit, head || recognised as source_query
    from (select distinct top1_bps from rows), lines
    union all
    select 'top5', top5_bps, 'bps', head || recognised
    from (select distinct top5_bps from rows), lines
    union all
    select 'top10', top10_bps, 'bps', head || recognised
    from (select distinct top10_bps from rows), lines
    union all
    select 'customer/' || customer_id, revenue_cents, 'cents',
        head || ' and customer_key.customer_id = ' || q || recognised
    from rows, lines
    union all
    select 'share/' || customer_id, share_bps, 'bps',
        head || ' and customer_key.customer_id = ' || q || recognised
    from rows, lines
)

select
    'concentration/' || $month || '/' || metric as number_id,
    $month as month,
    value::bigint as value_cents,
    null::varchar as value_text,
    unit,
    'leaf' as kind,
    source_query,
    null::varchar[] as children,
    'fct_concentration' as step
from leaves
order by number_id
