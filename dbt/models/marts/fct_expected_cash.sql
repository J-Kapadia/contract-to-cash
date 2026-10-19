-- Expected cash per open invoice per month-end and case.
--
-- An invoice is open at a month-end when it is dated on or before the last day of the month and its
-- lines' net total is not covered by the payments applied by then; the open amount is the difference.
-- Each open invoice gets three rows, dated by expected_pay_date() from the customer's days to pay in
-- fct_days_to_pay for that month: base takes the median, downside the 80th percentile and upside the
-- lesser of the median and the invoice's terms. A customer with no row there takes the pooled row
-- (customer_id '*'). A past date becomes the day after month-end plus the customer's median lateness
-- beyond due, the median of (paid date - due date) over the invoices paid in the trailing
-- `dtp_window_months` months, the pooled median for a customer with fewer than `dtp_min_paid_invoices`
-- of them; a negative median counts as none.
--
-- An invoice more than `at_risk_days_past_due` days past due at month-end is at risk. It brings no
-- cash in base or upside (cents 0); the downside keeps its open amount, flagged.
--
-- Where the books are silent: an invoice counts as paid by the same rule as fct_days_to_pay, only
-- here the paid date is the date its running total of applications first covered it; a customer
-- or month with no days to pay at all leaves the date on the invoice's terms.

with billing_customers as (
    select source_id as billing_customer_id, customer_id
    from {{ ref('int_customer_members') }}
    where source_system = 'billing'
),

invoices as (
    select
        invoice_id,
        min(billing_customer_id) as billing_customer_id,
        min(invoice_date) as invoice_date,
        min(due_date) as due_date,
        sum(amount_cents)::bigint as invoice_cents
    from {{ ref('stg_invoices') }}
    group by invoice_id
),

applications as (
    select
        invoice_id,
        applied_on,
        applied_cents,
        sum(applied_cents) over (
            partition by invoice_id order by applied_on, bank_txn_id, payment_id
            rows between unbounded preceding and current row
        ) as paid_cents
    from {{ ref('int_cash_application') }}
    where kind in ('applied', 'auto_applied') and invoice_id is not null
),

months as (
    select strftime(month_start, '%Y-%m') as month, month_start, last_day(month_start) as as_of
    from (
        select unnest(generate_series(
            (select min(date_trunc('month', invoice_date)) from invoices),
            (select max(strptime(month, '%Y-%m')) from (
                select strftime(invoice_date, '%Y-%m') as month from invoices
                union all select _month from {{ ref('stg_gl') }}
            )),
            interval 1 month
        )) as month_start
    )
),

applied_by_month_end as (
    select
        months.month,
        months.as_of,
        invoices.invoice_id,
        invoices.billing_customer_id,
        invoices.invoice_date,
        invoices.due_date,
        invoices.invoice_cents,
        coalesce(sum(applications.applied_cents), 0)::bigint as applied_cents
    from months
    inner join invoices on invoices.invoice_date <= months.as_of
    left join applications
        on applications.invoice_id = invoices.invoice_id and applications.applied_on <= months.as_of
    group by all
),

open_invoices as (
    select
        applied_by_month_end.month,
        applied_by_month_end.as_of,
        applied_by_month_end.invoice_id,
        billing_customers.customer_id,
        applied_by_month_end.invoice_date,
        applied_by_month_end.due_date,
        applied_by_month_end.invoice_cents - applied_by_month_end.applied_cents as open_cents
    from applied_by_month_end
    left join billing_customers using (billing_customer_id)
    where applied_by_month_end.invoice_cents > applied_by_month_end.applied_cents
),

paid as (
    select
        invoices.invoice_id,
        billing_customers.customer_id,
        min(applications.applied_on) as paid_on,
        min(applications.applied_on) - min(invoices.due_date) as days_late
    from invoices
    inner join applications using (invoice_id)
    left join billing_customers on billing_customers.billing_customer_id = invoices.billing_customer_id
    where applications.paid_cents >= invoices.invoice_cents
    group by invoices.invoice_id, billing_customers.customer_id
),

windowed as (
    select months.month, paid.customer_id, paid.invoice_id, paid.days_late
    from months
    inner join paid
        on date_trunc('month', paid.paid_on) <= months.month_start
        and date_trunc('month', paid.paid_on) > months.month_start - to_months({{ policy('dtp_window_months') }}::int)
),

grouped as (
    select month, customer_id, invoice_id, days_late from windowed where customer_id is not null
    union all
    select month, '*' as customer_id, invoice_id, days_late from windowed
),

ranked as (
    select
        month,
        customer_id,
        days_late,
        row_number() over (partition by month, customer_id order by days_late, invoice_id) as position,
        count(*) over (partition by month, customer_id) as paid_invoices
    from grouped
),

median_lateness as (
    select
        month,
        customer_id,
        max(paid_invoices) as paid_invoices,
        greatest(max(case when position = (paid_invoices - 1) // 2 + 1 then days_late end), 0)::int as lateness_days
    from ranked
    group by month, customer_id
),

lateness as (
    select
        own.month,
        own.customer_id,
        case
            when own.paid_invoices < {{ policy('dtp_min_paid_invoices') }} then pooled.lateness_days
            else own.lateness_days
        end as lateness_days
    from median_lateness as own
    inner join median_lateness as pooled on pooled.month = own.month and pooled.customer_id = '*'
    where own.customer_id <> '*'
),

dated as (
    select
        open_invoices.*,
        coalesce(own_dtp.median_days, pooled_dtp.median_days) as median_days,
        coalesce(own_dtp.p80_days, pooled_dtp.p80_days) as p80_days,
        coalesce(own_lateness.lateness_days, pooled_lateness.lateness_days) as lateness_days
    from open_invoices
    left join {{ ref('fct_days_to_pay') }} as own_dtp
        on own_dtp.month = open_invoices.month and own_dtp.customer_id = open_invoices.customer_id
    left join {{ ref('fct_days_to_pay') }} as pooled_dtp
        on pooled_dtp.month = open_invoices.month and pooled_dtp.customer_id = '*'
    left join lateness as own_lateness
        on own_lateness.month = open_invoices.month and own_lateness.customer_id = open_invoices.customer_id
    left join median_lateness as pooled_lateness
        on pooled_lateness.month = open_invoices.month and pooled_lateness.customer_id = '*'
),

cases as (
    select
        dated.*,
        cases."case",
        case cases."case"
            when 'base' then dated.median_days
            when 'downside' then dated.p80_days
            else least(dated.median_days, {{ expected_terms_days('dated.invoice_date', 'dated.due_date') }})
        end as case_days
    from dated
    cross join (values ('base'), ('downside'), ('upside')) as cases ("case")
)

select
    month,
    invoice_id,
    customer_id,
    "case",
    {{ expected_pay_date('invoice_date', 'due_date', 'as_of', 'case_days', 'lateness_days', source('raw', 'ref_business_days')) }} as expected_date,
    case when at_risk and "case" <> 'downside' then 0 else open_cents end::bigint as cents,
    at_risk
from (
    select
        cases.*,
        date_diff('day', due_date, as_of) > {{ policy('at_risk_days_past_due') }} as at_risk
    from cases
)
order by month, invoice_id, "case"
