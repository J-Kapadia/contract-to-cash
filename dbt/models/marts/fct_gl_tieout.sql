-- GL account 1200 (accounts receivable) against the AR subledger: the ledger's balance at each
-- month end (every line posted in a period up to it) against the roll-forward's closing total.

with ledger as (
    select gl_period as month, sum(amount_cents) as movement_cents
    from {{ ref('stg_gl') }}
    where gl_account = '1200'
    group by gl_period
),

subledger as (
    select month, sum(closing_cents) as subledger_cents
    from {{ ref('fct_ar_rollforward') }}
    group by month
),

months as (
    select month from subledger
    union
    select month from ledger
),

balances as (
    select
        months.month,
        sum(coalesce(ledger.movement_cents, 0)) over (order by months.month) as gl_cents,
        coalesce(subledger.subledger_cents, 0) as subledger_cents
    from months
    left join ledger using (month)
    left join subledger using (month)
)

select
    month,
    '1200' as gl_account,
    gl_cents::bigint as gl_cents,
    subledger_cents::bigint as subledger_cents,
    (gl_cents - subledger_cents)::bigint as diff_cents
from balances
order by month
