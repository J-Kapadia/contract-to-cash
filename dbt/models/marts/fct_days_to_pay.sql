-- Days to pay per customer per month: the lower median and the nearest-rank 80th percentile of
-- (the date the invoice became fully paid) - (invoice date) over the invoices paid in the trailing
-- `dtp_window_months` months, the month itself included.
--
-- An invoice counts once the payments applied to it cover its lines' net total; its paid date is the
-- application date on which the running total first covered it, so an invoice paid in instalments
-- takes the date of the last one it needed. An invoice never covered is left out.
--
-- A customer with fewer than `dtp_min_paid_invoices` paid invoices in the window takes the pooled
-- values and is marked `fallback`. The pooled row has customer_id '*'. A customer with no paid invoice
-- in the window has no row, and a month with no paid invoice at all has none.
--
-- Where the books are silent: the order statistics rank the days with ties by invoice id, which does
-- not change a value; the percentile is the nearest rank of 80.

with invoices as (
    select
        invoice_id,
        min(invoice_date) as invoice_date,
        sum(amount_cents)::bigint as invoice_cents
    from {{ ref('stg_invoices') }}
    group by invoice_id
),

applications as (
    select
        invoice_id,
        customer_id,
        applied_on,
        sum(applied_cents) over (
            partition by invoice_id order by applied_on, bank_txn_id, payment_id
            rows between unbounded preceding and current row
        ) as paid_cents
    from {{ ref('int_cash_application') }}
    where kind in ('applied', 'auto_applied') and invoice_id is not null
),

paid as (
    select
        invoices.invoice_id,
        min(applications.customer_id) as customer_id,
        min(applications.applied_on) as paid_on,
        min(applications.applied_on) - min(invoices.invoice_date) as days
    from invoices
    inner join applications using (invoice_id)
    where applications.paid_cents >= invoices.invoice_cents
    group by invoices.invoice_id
),

months as (
    select strftime(month_start, '%Y-%m') as month, month_start
    from (
        select unnest(generate_series(
            (select min(date_trunc('month', paid_on)) from paid),
            (select max(strptime(month, '%Y-%m')) from (
                select strftime(paid_on, '%Y-%m') as month from paid
                union all select _month from {{ ref('stg_gl') }}
            )),
            interval 1 month
        )) as month_start
    )
),

windowed as (
    select months.month, paid.customer_id, paid.invoice_id, paid.days
    from months
    inner join paid
        on date_trunc('month', paid.paid_on) <= months.month_start
        and date_trunc('month', paid.paid_on) > months.month_start - to_months({{ policy('dtp_window_months') }}::int)
),

grouped as (
    select month, customer_id, invoice_id, days from windowed where customer_id is not null
    union all
    select month, '*' as customer_id, invoice_id, days from windowed
),

ranked as (
    select
        month,
        customer_id,
        days,
        row_number() over (partition by month, customer_id order by days, invoice_id) as position,
        count(*) over (partition by month, customer_id) as paid_invoices
    from grouped
),

statistics as (
    select
        month,
        customer_id,
        max(paid_invoices)::int as paid_invoices,
        max(case when position = (paid_invoices - 1) // 2 + 1 then days end)::int as median_days,
        max(case when position = greatest((80 * paid_invoices + 99) // 100, 1) then days end)::int as p80_days
    from ranked
    group by month, customer_id
),

compared as (
    select
        own.month,
        own.customer_id,
        own.paid_invoices,
        own.median_days,
        own.p80_days,
        pooled.median_days as pooled_median_days,
        pooled.p80_days as pooled_p80_days,
        own.customer_id <> '*' and own.paid_invoices < {{ policy('dtp_min_paid_invoices') }} as fallback
    from statistics as own
    inner join statistics as pooled on pooled.month = own.month and pooled.customer_id = '*'
)

select
    month,
    customer_id,
    paid_invoices,
    case when fallback then pooled_median_days else median_days end as median_days,
    case when fallback then pooled_p80_days else p80_days end as p80_days,
    fallback
from compared
order by month, customer_id
