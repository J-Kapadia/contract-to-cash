-- Accounts receivable per customer per month: opening + billed - collected - credits = closing.
-- The customer is the contract's CRM account; a payment reaches its contract through its invoice.
-- Every customer has a row for every month from the first month with activity to the last.

with contract_customers as (
    select contract_id, min(crm_account_id) as customer_id
    from {{ source('raw', 'contracts') }}
    group by contract_id
),

invoice_contracts as (
    select invoice_id, min(contract_id) as contract_id
    from {{ ref('stg_invoices') }}
    group by invoice_id
),

movements as (
    select
        contract_customers.customer_id,
        stg_invoices._month as month,
        sum(case when stg_invoices.line_type = 'credit' then 0 else stg_invoices.amount_cents end) as billed_cents,
        0::bigint as collected_cents,
        sum(case when stg_invoices.line_type = 'credit' then -stg_invoices.amount_cents else 0 end) as credits_cents
    from {{ ref('stg_invoices') }}
    left join contract_customers using (contract_id)
    group by all

    union all

    select
        contract_customers.customer_id,
        stg_payments._month as month,
        0::bigint as billed_cents,
        sum(stg_payments.applied_cents) as collected_cents,
        0::bigint as credits_cents
    from {{ ref('stg_payments') }}
    left join invoice_contracts using (invoice_id)
    left join contract_customers using (contract_id)
    group by all
),

monthly as (
    select
        customer_id,
        month,
        sum(billed_cents)::bigint as billed_cents,
        sum(collected_cents)::bigint as collected_cents,
        sum(credits_cents)::bigint as credits_cents
    from movements
    group by all
),

months as (
    select strftime(month_start, '%Y-%m') as month
    from (
        select unnest(generate_series(
            (select min(strptime(month, '%Y-%m')) from monthly),
            (select max(strptime(month, '%Y-%m')) from (
                select month from monthly
                union all select _month from {{ ref('stg_gl') }}
            )),
            interval 1 month
        )) as month_start
    )
),

customers as (
    select distinct customer_id from monthly
),

spine as (
    select customers.customer_id, months.month
    from customers cross join months
),

filled as (
    select
        spine.customer_id,
        spine.month,
        coalesce(monthly.billed_cents, 0) as billed_cents,
        coalesce(monthly.collected_cents, 0) as collected_cents,
        coalesce(monthly.credits_cents, 0) as credits_cents
    from spine
    left join monthly
        on monthly.month = spine.month
        and monthly.customer_id is not distinct from spine.customer_id
),

rolled as (
    select
        *,
        sum(billed_cents - collected_cents - credits_cents) over (
            partition by customer_id order by month
        ) as closing_cents
    from filled
)

select
    month,
    customer_id,
    (closing_cents - billed_cents + collected_cents + credits_cents)::bigint as opening_cents,
    billed_cents,
    collected_cents,
    credits_cents,
    billed_cents as additions_cents,
    (collected_cents + credits_cents)::bigint as reductions_cents,
    closing_cents::bigint as closing_cents
from rolled
order by month, customer_id
