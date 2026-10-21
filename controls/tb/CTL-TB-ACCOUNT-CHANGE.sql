-- remediation: Account {account_id} {account_name} {change} in {period} against {previous_period}: confirm the chart of accounts change or correct the export.
-- stage: tb
-- This control lives in controls/tb/, not controls/: c2c load-tb runs the stage `tb` over that directory,
-- and the close globs controls/ without recursion, so the close never runs it and the control-id and
-- remediation tests, which read controls/ the same way, do not see it.
-- Its id is its own: CTL-DRIFT-ACCOUNT stays the close-time control on the ledger. Each period is
-- compared with the previous period present in the stack; the first period reports nothing.

with periods as (
    select period, lag(period) over (order by period) as previous_period
    from (select distinct period from mart.fct_trial_balance)
),
accounts as (
    select period, account_id, any_value(account_name) as account_name,
        list(_row_hash order by _row_hash) as _row_hash
    from mart.fct_trial_balance
    group by period, account_id
),
changes as (
    select a.account_id, a.account_name, 'appears' as change, p.period, p.previous_period, a._row_hash
    from periods as p
    join accounts as a on a.period = p.period
    where p.previous_period is not null
        and not exists (select 1 from accounts as b where b.period = p.previous_period and b.account_id = a.account_id)
    union all
    select b.account_id, b.account_name, 'disappears' as change, p.period, p.previous_period, b._row_hash
    from periods as p
    join accounts as b on b.period = p.previous_period
    where not exists (select 1 from accounts as a where a.period = p.period and a.account_id = b.account_id)
)
select account_id, account_name, change, period, previous_period, _row_hash
from changes
order by period, account_id, change
