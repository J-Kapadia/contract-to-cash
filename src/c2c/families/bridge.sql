-- The walk from revenue to receipts: each line is a composite over the month's numbers it adds up.
-- Revenue is its recognised-revenue numbers, a deferred-revenue or receivables movement the opening
-- and closing of each customer, net billings revenue plus the deferred-revenue movement, and receipts
-- all three.
with rev as (
    select coalesce(list('rev/' || month || '/' || customer_id || '/' || line_type order by customer_id, line_type),
                    []::varchar[]) as ids
    from mart.fct_revenue
    where month = $month and customer_id is not null
),

defrev as (
    select coalesce(list('defrev/' || month || '/' || customer_id || '/' || metric order by customer_id, metric),
                    []::varchar[]) as ids
    from mart.fct_defrev_rollforward
    cross join (values ('opening'), ('closing')) as metrics (metric)
    where month = $month and customer_id is not null
),

ar as (
    select coalesce(list('ar/' || month || '/' || customer_id || '/' || metric order by customer_id, metric),
                    []::varchar[]) as ids
    from mart.fct_ar_rollforward
    cross join (values ('opening'), ('closing')) as metrics (metric)
    where month = $month and customer_id is not null
)

select
    'bridge/' || month || '/' || line as number_id,
    month,
    cents as value_cents,
    null::varchar as value_text,
    'cents' as unit,
    'composite' as kind,
    null::varchar as source_query,
    case line
        when 'revenue' then rev.ids
        when 'defrev_movement' then defrev.ids
        when 'net_billings' then list_concat(rev.ids, defrev.ids)
        when 'ar_movement' then ar.ids
        else list_concat(list_concat(rev.ids, defrev.ids), ar.ids)
    end as children,
    'fct_bridge' as step
from mart.fct_bridge
cross join rev
cross join defrev
cross join ar
where month = $month
order by number_id
