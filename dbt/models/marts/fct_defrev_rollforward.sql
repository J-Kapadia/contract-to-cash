-- Deferred revenue per customer per month: opening + billings - recognised = closing.
--
-- Billings are the subscription and credit lines of the invoices dated in the month (a credit line is
-- a negative billing); overage is billed and recognised in the same month, so it never sits here.
-- Recognised is the subscription and credit revenue of the month from fct_revenue. Every customer
-- has a row for every month from the first month with a billing or recognition to the last month
-- with any activity.
--
-- Where the contracts are silent: the credit lines' recognition is taken off alongside the
-- subscription lines', because a credit's billing is added here.

with customer_key as (
    select contract_id, min(crm_account_id) as customer_id
    from {{ ref('stg_contracts') }}
    group by contract_id
),

billings as (
    select
        customer_key.customer_id,
        strftime(stg_invoices.invoice_date, '%Y-%m') as month,
        sum(stg_invoices.amount_cents)::bigint as additions_cents
    from {{ ref('stg_invoices') }}
    left join customer_key using (contract_id)
    where stg_invoices.line_type in ('subscription', 'credit')
    group by all
),

recognised as (
    select
        month,
        customer_id,
        sum(recognised_cents)::bigint as reductions_cents
    from {{ ref('fct_revenue') }}
    where line_type in ('subscription', 'credit')
    group by all
),

activity as (
    select _month as month from {{ ref('stg_invoices') }}
    union all select _month from {{ ref('stg_payments') }}
    union all select _month from {{ ref('stg_gl') }}
),

months as (
    select strftime(month_start, '%Y-%m') as month
    from (
        select unnest(generate_series(
            (select min(strptime(month, '%Y-%m')) from (
                select month from billings union all select month from recognised
            )),
            (select max(strptime(month, '%Y-%m')) from activity),
            interval 1 month
        )) as month_start
    )
),

customers as (
    select customer_id from billings
    union
    select customer_id from recognised
),

spine as (
    select customers.customer_id, months.month
    from customers cross join months
),

filled as (
    select
        spine.customer_id,
        spine.month,
        coalesce(billings.additions_cents, 0) as additions_cents,
        coalesce(recognised.reductions_cents, 0) as reductions_cents
    from spine
    left join billings
        on billings.month = spine.month
        and billings.customer_id is not distinct from spine.customer_id
    left join recognised
        on recognised.month = spine.month
        and recognised.customer_id is not distinct from spine.customer_id
),

rolled as (
    select
        *,
        sum(additions_cents - reductions_cents) over (
            partition by customer_id order by month
        ) as closing_cents
    from filled
)

select
    month,
    customer_id,
    (closing_cents - additions_cents + reductions_cents)::bigint as opening_cents,
    additions_cents::bigint as additions_cents,
    reductions_cents::bigint as reductions_cents,
    closing_cents::bigint as closing_cents
from rolled
order by month, customer_id
