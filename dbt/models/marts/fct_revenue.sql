-- Recognised revenue per customer, month and line type, from the invoice lines.
--
-- Subscription and credit lines are recognised by day over their service period through
-- ratable_cents(); an overage line is recognised whole in the month its service starts (the usage
-- month). The months run from the first month with an invoice, payment or ledger line to the last,
-- so a period that runs past the last month is recognised only up to it.
--
-- Where the contracts are silent: a month in which a customer recognises nothing has no row.

with customer_key as (
    select
        stg_invoices.contract_id,
        coalesce(min(members.customer_id), min(contracts.crm_account_id)) as customer_id
    from {{ ref('stg_invoices') }}
    inner join {{ ref('int_customer_members') }} as members
        on members.source_system = 'billing' and members.source_id = stg_invoices.billing_customer_id
    left join {{ ref('stg_contracts') }} as contracts on contracts.contract_id = stg_invoices.contract_id
    group by stg_invoices.contract_id
),

activity as (
    select _month as month from {{ ref('stg_invoices') }}
    union all select _month from {{ ref('stg_payments') }}
    union all select _month from {{ ref('stg_gl') }}
),

spine as (
    select strftime(month_start, '%Y-%m') as month
    from (
        select unnest(generate_series(
            (select min(strptime(month, '%Y-%m')) from activity),
            (select max(strptime(month, '%Y-%m')) from activity),
            interval 1 month
        )) as month_start
    )
),

lines as (
    select
        customer_key.customer_id,
        stg_invoices.line_type,
        stg_invoices.amount_cents,
        stg_invoices.service_start,
        stg_invoices.service_end,
        strftime(stg_invoices.service_start, '%Y-%m') as first_month,
        case stg_invoices.line_type
            when 'overage' then strftime(stg_invoices.service_start, '%Y-%m')
            else strftime(stg_invoices.service_end, '%Y-%m')
        end as last_month
    from {{ ref('stg_invoices') }}
    left join customer_key using (contract_id)
),

recognised as (
    select
        spine.month,
        lines.customer_id,
        lines.line_type,
        case lines.line_type
            when 'overage' then lines.amount_cents
            else {{ ratable_cents('lines.amount_cents', 'lines.service_start', 'lines.service_end', 'spine.month') }}
        end as cents
    from lines
    inner join spine on spine.month between lines.first_month and lines.last_month
)

select
    month,
    customer_id,
    line_type,
    sum(cents)::bigint as recognised_cents
from recognised
group by all
order by month, customer_id, line_type
