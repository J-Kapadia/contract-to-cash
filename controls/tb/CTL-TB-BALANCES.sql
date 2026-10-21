-- remediation: Trial balance {period} does not balance: debits exceed credits by {difference_cents} cents; correct the {period} export.
-- stage: tb
-- This control lives in controls/tb/, not controls/: c2c load-tb runs the stage `tb` over that directory,
-- and the close globs controls/ without recursion, so the close never runs it and the control-id and
-- remediation tests, which read controls/ the same way, do not see it.
-- Debits equal credits in every month; a negative difference means credits exceed debits.

select
    period,
    sum(debit_cents) - sum(credit_cents) as difference_cents,
    list(_row_hash order by _row_hash) as _row_hash
from mart.fct_trial_balance
group by period
having sum(debit_cents) - sum(credit_cents) <> 0
order by period
