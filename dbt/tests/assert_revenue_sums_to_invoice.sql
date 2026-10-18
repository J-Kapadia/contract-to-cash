-- Revenue is conserved: the months of every subscription and credit line add up to its amount, and
-- a customer whose lines of a type all end within the months fct_revenue covers has recognised
-- exactly what was invoiced. Each returned row is a violation.
--
-- dbt selects a test when any of its parents is selected, so a build of the billing schedule alone
-- reaches this test before fct_revenue exists; with no fct_revenue there is nothing to check.
{% if load_relation(ref('fct_revenue')) is none %}
select 'line' as scope, '' as subject, 0::bigint as invoiced_cents, 0::bigint as recognised_cents
where false
{% else %}

with customer_key as (
    select contract_id, min(crm_account_id) as customer_id
    from {{ source('raw', 'contracts') }}
    group by contract_id
),

months as (
    select
        invoices.invoice_id,
        invoices.line_no,
        customer_key.customer_id,
        invoices.line_type,
        invoices.amount_cents::bigint as amount_cents,
        invoices.service_start::date as service_start,
        invoices.service_end::date as service_end,
        strftime(unnest(generate_series(
            date_trunc('month', invoices.service_start::date),
            date_trunc('month', invoices.service_end::date),
            interval 1 month
        )), '%Y-%m') as month
    from {{ source('raw', 'invoices') }} as invoices
    left join customer_key using (contract_id)
    where invoices.line_type <> 'overage'
),

per_line as (
    select
        invoice_id,
        line_no,
        customer_id,
        line_type,
        amount_cents,
        strftime(service_end, '%Y-%m') as end_month,
        sum({{ ratable_cents('amount_cents', 'service_start', 'service_end', 'month') }}) as recognised_cents
    from months
    group by all
),

line_violations as (
    select
        'line' as scope,
        invoice_id || '/' || line_no as subject,
        amount_cents as invoiced_cents,
        recognised_cents
    from per_line
    where recognised_cents <> amount_cents
),

covered as (
    select customer_id, line_type, sum(amount_cents) as invoiced_cents
    from per_line
    group by customer_id, line_type
    having max(end_month) <= (select max(month) from {{ ref('fct_revenue') }})
),

recognised as (
    select customer_id, line_type, sum(recognised_cents) as recognised_cents
    from {{ ref('fct_revenue') }}
    group by customer_id, line_type
),

total_violations as (
    select
        'customer' as scope,
        covered.customer_id || '/' || covered.line_type as subject,
        covered.invoiced_cents,
        coalesce(recognised.recognised_cents, 0) as recognised_cents
    from covered
    left join recognised using (customer_id, line_type)
    where covered.invoiced_cents <> coalesce(recognised.recognised_cents, 0)
)

select * from line_violations
union all
select * from total_violations
{% endif %}
