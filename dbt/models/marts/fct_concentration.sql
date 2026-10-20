-- Customer concentration per month: each customer's recognised revenue, its rank and its share of the
-- month's total in basis points (floor), and the shares of the largest one, five and ten customers,
-- repeated on every row of the month. A month whose total is 0, or that has no customer, has no rows.
-- Where the books are silent: a customer whose lines net to zero in the month has no row, and a negative
-- customer ranks below every positive one.
with per_customer as (
    select
        month,
        customer_id,
        sum(recognised_cents)::bigint as revenue_cents
    from {{ ref('fct_revenue') }}
    where customer_id is not null
    group by month, customer_id
    having sum(recognised_cents) <> 0
),

ranked as (
    select
        month,
        customer_id,
        revenue_cents,
        sum(revenue_cents) over (partition by month)::bigint as total_cents,
        row_number() over (partition by month order by revenue_cents desc, customer_id)::bigint as rank
    from per_customer
),

cumulated as (
    select
        *,
        sum(revenue_cents) filter (where rank <= 1) over (partition by month)::bigint as top1_cents,
        sum(revenue_cents) filter (where rank <= 5) over (partition by month)::bigint as top5_cents,
        sum(revenue_cents) filter (where rank <= 10) over (partition by month)::bigint as top10_cents
    from ranked
)

select
    month,
    customer_id,
    revenue_cents,
    total_cents,
    rank,
    (revenue_cents * 10000 // total_cents)::bigint as share_bps, -- single-definition: ok a share in basis points
    (top1_cents * 10000 // total_cents)::bigint as top1_bps, -- single-definition: ok a share in basis points
    (top5_cents * 10000 // total_cents)::bigint as top5_bps, -- single-definition: ok a share in basis points
    (top10_cents * 10000 // total_cents)::bigint as top10_bps -- single-definition: ok a share in basis points
from cumulated
where total_cents <> 0
order by month, rank
