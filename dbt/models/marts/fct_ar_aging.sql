-- Open receivables per customer and month-end, by days past due: what each invoice billed through the
-- month still owes after the payments applied through the month, in the bucket of its due date.
--
-- The edges are `aging_buckets_days` (0, 30, 60, 90): an invoice not past due at month-end (days past
-- due 0 or less) is `current`; then `1-30`, `31-60`, `61-90` and `91+`. An invoice's due date is the
-- earliest of its lines'; its balance is its lines' net total (credit lines included) less its applications.
-- Every customer-month of the receivables roll-forward has a row for every bucket, so the buckets of
-- a customer add up to its closing balance there.
--
-- Where the books are silent: cash applied to no invoice of the books (no invoice id) has no due date
-- and is carried in `current`; a month-end is the last day of the month.

with billing_customers as (
    select source_id as billing_customer_id, customer_id
    from {{ ref('int_customer_members') }}
    where source_system = 'billing'
),

invoices as (
    select
        stg_invoices.invoice_id,
        billing_customers.customer_id,
        min(stg_invoices.due_date) as due_date,
        min(stg_invoices._month) as month,
        sum(stg_invoices.amount_cents)::bigint as invoice_cents
    from {{ ref('stg_invoices') }}
    left join billing_customers using (billing_customer_id)
    group by stg_invoices.invoice_id, billing_customers.customer_id
),

applications as (
    select
        invoice_id,
        strftime(applied_on, '%Y-%m') as month,
        sum(applied_cents)::bigint as applied_cents
    from {{ ref('int_cash_application') }}
    where kind in ('applied', 'auto_applied')
    group by all
),

months as (
    select distinct month from {{ ref('fct_ar_rollforward') }}
),

balances as (
    select
        months.month,
        invoices.customer_id,
        (last_day(strptime(months.month, '%Y-%m')) - invoices.due_date) as days_past_due,
        invoices.invoice_cents - coalesce(sum(applications.applied_cents), 0) as open_cents
    from months
    inner join invoices on invoices.month <= months.month
    left join applications
        on applications.invoice_id = invoices.invoice_id and applications.month <= months.month
    group by months.month, invoices.invoice_id, invoices.customer_id, invoices.due_date, invoices.invoice_cents
),

unapplied as (
    select
        months.month,
        int_cash_application.customer_id,
        0 as days_past_due,
        -sum(int_cash_application.applied_cents)::bigint as open_cents
    from months
    inner join {{ ref('int_cash_application') }} as int_cash_application
        on strftime(int_cash_application.applied_on, '%Y-%m') <= months.month
    where int_cash_application.kind in ('applied', 'auto_applied')
      and coalesce(int_cash_application.invoice_id, '') not in (select invoice_id from invoices)
    group by all
),

bucketed as (
    select
        month,
        customer_id,
        case
            when days_past_due <= {{ policy('aging_buckets_days.0') }} then 'current'
            when days_past_due <= {{ policy('aging_buckets_days.1') }}
                then ({{ policy('aging_buckets_days.0') }} + 1) || '-' || {{ policy('aging_buckets_days.1') }}
            when days_past_due <= {{ policy('aging_buckets_days.2') }}
                then ({{ policy('aging_buckets_days.1') }} + 1) || '-' || {{ policy('aging_buckets_days.2') }}
            when days_past_due <= {{ policy('aging_buckets_days.3') }}
                then ({{ policy('aging_buckets_days.2') }} + 1) || '-' || {{ policy('aging_buckets_days.3') }}
            else ({{ policy('aging_buckets_days.3') }} + 1) || '+'
        end as bucket,
        open_cents
    from (select * from balances union all select * from unapplied)
),

buckets as (
    select unnest([
        'current',
        ({{ policy('aging_buckets_days.0') }} + 1) || '-' || {{ policy('aging_buckets_days.1') }},
        ({{ policy('aging_buckets_days.1') }} + 1) || '-' || {{ policy('aging_buckets_days.2') }},
        ({{ policy('aging_buckets_days.2') }} + 1) || '-' || {{ policy('aging_buckets_days.3') }},
        ({{ policy('aging_buckets_days.3') }} + 1) || '+'
    ]) as bucket
),

spine as (
    select distinct month, customer_id from {{ ref('fct_ar_rollforward') }}
)

select
    spine.month,
    spine.customer_id,
    buckets.bucket,
    coalesce(sum(bucketed.open_cents), 0)::bigint as open_cents
from spine
cross join buckets
left join bucketed
    on bucketed.month = spine.month
    and bucketed.bucket = buckets.bucket
    and bucketed.customer_id is not distinct from spine.customer_id
group by spine.month, spine.customer_id, buckets.bucket
order by spine.month, spine.customer_id, buckets.bucket
