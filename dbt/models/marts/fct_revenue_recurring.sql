-- Recognised revenue per month split into recurring and non-recurring, from the invoice lines.
--
-- A line is recurring when it is a subscription line that refers to a contract line in the books;
-- overage lines, credit lines and subscription lines with a blank or unknown contract line are
-- non-recurring. Recognition is the same as fct_revenue's: subscription and credit lines by day over
-- their service period through ratable_cents(), an overage line whole in the month its service
-- starts, over the same months. Both classifications are present for every month, 0 where nothing
-- is recognised.

with activity as (
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

classifications as (
    select * from (values ('recurring'), ('non_recurring')) as t (classification)
),

lines as (
    select
        stg_invoices.line_type,
        stg_invoices.amount_cents,
        stg_invoices.service_start,
        stg_invoices.service_end,
        strftime(stg_invoices.service_start, '%Y-%m') as first_month,
        case stg_invoices.line_type
            when 'overage' then strftime(stg_invoices.service_start, '%Y-%m')
            else strftime(stg_invoices.service_end, '%Y-%m')
        end as last_month,
        case
            when stg_invoices.line_type = 'subscription' and exists (
                select 1
                from {{ ref('stg_contract_lines') }} as contract_lines
                where contract_lines.contract_id = stg_invoices.contract_id
                    and contract_lines.line_id = stg_invoices.contract_line_id
            ) then 'recurring'
            else 'non_recurring'
        end as classification
    from {{ ref('stg_invoices') }}
),

recognised as (
    select
        spine.month,
        lines.classification,
        case lines.line_type
            when 'overage' then lines.amount_cents
            else {{ ratable_cents('lines.amount_cents', 'lines.service_start', 'lines.service_end', 'spine.month') }}
        end as cents
    from lines
    inner join spine on spine.month between lines.first_month and lines.last_month
)

select
    spine.month,
    classifications.classification,
    coalesce(sum(recognised.cents), 0)::bigint as recognised_cents
from spine
cross join classifications
left join recognised
    on recognised.month = spine.month and recognised.classification = classifications.classification
group by all
order by month, classification
