-- Cash per month: opening + receipts - disbursements = closing. Receipts are the month's positive
-- bank amounts, disbursements its negative ones (as a positive figure). The first month opens at
-- the bank's opening-balance row, which is not itself a receipt. Every month from the first to the
-- last with a bank or ledger line has a row.

with opening as (
    select coalesce(sum(amount_cents), 0) as opening_cents
    from {{ ref('stg_bank_transactions') }}
    where is_opening_balance
),

movements as (
    select
        _month as month,
        sum(case when amount_cents > 0 then amount_cents else 0 end)::bigint as receipts_cents,
        sum(case when amount_cents < 0 then -amount_cents else 0 end)::bigint as disbursements_cents
    from {{ ref('stg_bank_transactions') }}
    where not is_opening_balance
    group by _month
),

months as (
    select strftime(month_start, '%Y-%m') as month
    from (
        select unnest(generate_series(
            (select min(strptime(month, '%Y-%m')) from (
                select _month as month from {{ ref('stg_bank_transactions') }}
                union all select _month from {{ ref('stg_gl') }}
            )),
            (select max(strptime(month, '%Y-%m')) from (
                select _month as month from {{ ref('stg_bank_transactions') }}
                union all select _month from {{ ref('stg_gl') }}
            )),
            interval 1 month
        )) as month_start
    )
),

filled as (
    select
        months.month,
        coalesce(movements.receipts_cents, 0) as receipts_cents,
        coalesce(movements.disbursements_cents, 0) as disbursements_cents
    from months
    left join movements using (month)
),

rolled as (
    select
        *,
        (select opening_cents from opening)
            + sum(receipts_cents - disbursements_cents) over (order by month) as closing_cents
    from filled
)

select
    month,
    (closing_cents - receipts_cents + disbursements_cents)::bigint as opening_cents,
    receipts_cents,
    disbursements_cents,
    closing_cents::bigint as closing_cents
from rolled
order by month
