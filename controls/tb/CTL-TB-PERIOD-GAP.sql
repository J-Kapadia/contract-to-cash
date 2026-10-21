-- remediation: No trial balance for {period}: add the {period} export, which lies between {first_period} and {last_period}.
-- stage: tb
-- This control lives in controls/tb/, not controls/: c2c load-tb runs the stage `tb` over that directory,
-- and the close globs controls/ without recursion, so the close never runs it and the control-id and
-- remediation tests, which read controls/ the same way, do not see it.
-- Every month from the first to the last period of the stack has rows.

with bounds as (
    select min(period) as first_period, max(period) as last_period
    from mart.fct_trial_balance
),
months as (
    select
        strftime(g.m, '%Y-%m') as period,
        b.first_period,
        b.last_period
    from bounds as b,
        generate_series(
            cast(b.first_period || '-01' as date),
            cast(b.last_period || '-01' as date),
            interval 1 month) as g(m)
)
select period, first_period, last_period
from months
where period not in (select period from mart.fct_trial_balance)
order by period
