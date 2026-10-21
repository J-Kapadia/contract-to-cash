-- The general ledger against the sub-ledgers, one row per month and account:
--   1000 cash                  closing balance of fct_cash_rollforward
--   1200 accounts receivable   closing balance of fct_ar_rollforward, summed over customers
--   2400 deferred revenue      closing balance of fct_defrev_rollforward, summed over customers
--   4000 subscription revenue  the month's recognised subscription and credit revenue
--   4100 usage revenue         the month's recognised overage revenue
-- gl_cents is the ledger's running balance for 1000, 1200 and 2400 (every line posted in a period up
-- to the month) and the period's activity for 4000 and 4100. A credit-balance account (2400, 4000,
-- 4100) is shown with the sign of its sub-ledger, i.e. the ledger's debit-positive amount negated.
-- diff_cents = gl_cents - subledger_cents. entry_ids lists the account's ledger entries of the month.
--
-- With a stacked trial balance in the warehouse (mart.fct_trial_balance, written by `c2c load-tb`), tb_cents
-- is the trial balance's figure for the account in the sub-ledger's sign and tb_diff_cents = tb_cents -
-- subledger_cents, both NULL for a month with no trial balance period. A balance account (1000, 1200, 2400)
-- takes its net_cents of the period equal to the month. A revenue account (4000, 4100) takes its net_cents
-- of the period minus the previous period's, because a trial balance carries year-to-date balances for
-- income accounts; the first period present takes its net as is, and a fiscal-year reset shows as a
-- one-month difference. An account with no row in a period present counts 0 there. A warehouse without
-- the trial balance gets both columns NULL and the same rows as before.
--
-- Where the plan is silent: a month a sub-ledger has no row for counts as 0 there; the months are
-- every month any of the five sources has, the same for every account.

{% set tb = execute and adapter.get_relation(database=target.database, schema='mart', identifier='fct_trial_balance') -%}

with ledger as (
    select
        gl_period as month,
        gl_account,
        sum(amount_cents) as movement_cents,
        list(distinct entry_id order by entry_id) as entry_ids
    from {{ ref('stg_gl') }}
    where gl_account in ('1000', '1200', '2400', '4000', '4100')
    group by gl_period, gl_account
),

subledger as (
    select month, '1000' as gl_account, sum(closing_cents) as subledger_cents
    from {{ ref('fct_cash_rollforward') }}
    group by month
    union all
    select month, '1200', sum(closing_cents)
    from {{ ref('fct_ar_rollforward') }}
    group by month
    union all
    select month, '2400', sum(closing_cents)
    from {{ ref('fct_defrev_rollforward') }}
    group by month
    union all
    select
        month,
        case when line_type = 'overage' then '4100' else '4000' end,
        sum(recognised_cents)
    from {{ ref('fct_revenue') }}
    group by all
),

accounts as (
    select * from (values ('1000', true, 1), ('1200', true, 1), ('2400', true, -1),
                          ('4000', false, -1), ('4100', false, -1)) as t (gl_account, is_balance, sign)
),

months as (
    select month from ledger
    union
    select month from subledger
),

spine as (
    select months.month, accounts.gl_account, accounts.is_balance, accounts.sign
    from months cross join accounts
),

joined as (
    select
        spine.month,
        spine.gl_account,
        spine.is_balance,
        spine.sign * coalesce(ledger.movement_cents, 0) as movement_cents,
        coalesce(subledger.subledger_cents, 0) as subledger_cents,
        coalesce(ledger.entry_ids, []) as entry_ids
    from spine
    left join ledger using (month, gl_account)
    left join subledger using (month, gl_account)
),

balances as (
    select
        *,
        case when is_balance
            then sum(movement_cents) over (partition by gl_account order by month)
            else movement_cents
        end as gl_cents
    from joined
)

{%- if tb %},

tb_periods as (
    select distinct period as month from mart.fct_trial_balance
),

tb_nets as (
    select period as month, account_id as gl_account, sum(net_cents) as net_cents
    from mart.fct_trial_balance
    group by period, account_id
),

tb_spine as (
    select
        tb_periods.month,
        accounts.gl_account,
        accounts.is_balance,
        accounts.sign,
        coalesce(tb_nets.net_cents, 0) as net_cents
    from tb_periods
    cross join accounts
    left join tb_nets
        on tb_nets.month = tb_periods.month and tb_nets.gl_account = accounts.gl_account
),

tb_figures as (
    select
        month,
        gl_account,
        sign * case when is_balance
            then net_cents
            else net_cents - coalesce(lag(net_cents) over (partition by gl_account order by month), 0)
        end as tb_cents
    from tb_spine
)
{%- endif %}

select
    balances.month,
    balances.gl_account,
    balances.gl_cents::bigint as gl_cents,
    balances.subledger_cents::bigint as subledger_cents,
    (balances.gl_cents - balances.subledger_cents)::bigint as diff_cents,
    balances.entry_ids,
{%- if tb %}
    tb_figures.tb_cents::bigint as tb_cents,
    (tb_figures.tb_cents - balances.subledger_cents)::bigint as tb_diff_cents
from balances
left join tb_figures on tb_figures.month = balances.month and tb_figures.gl_account = balances.gl_account
{%- else %}
    null::bigint as tb_cents,
    null::bigint as tb_diff_cents
from balances
{%- endif %}
order by balances.month, balances.gl_account
