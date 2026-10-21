-- remediation: The Total row of the {period} trial balance says debits {total_debit_cents} and credits {total_credit_cents} cents, but its account rows sum to {debit_cents} and {credit_cents}; correct the {period} export.
-- stage: tb
-- This control lives in controls/tb/, not controls/: c2c load-tb runs the stage `tb` over that directory,
-- and the close globs controls/ without recursion, so the close never runs it and the control-id and
-- remediation tests, which read controls/ the same way, do not see it.
-- A file without a Total row has no row in the totals table and is not compared.

select
    t.period,
    t.total_debit_cents,
    t.total_credit_cents,
    coalesce(sum(f.debit_cents), 0) as debit_cents,
    coalesce(sum(f.credit_cents), 0) as credit_cents,
    coalesce(list(f._row_hash order by f._row_hash) filter (where f._row_hash is not null), []) as _row_hash
from mart.fct_trial_balance_totals as t
left join mart.fct_trial_balance as f on f.period = t.period
group by t.period, t.total_debit_cents, t.total_credit_cents
having t.total_debit_cents is distinct from coalesce(sum(f.debit_cents), 0)
    or t.total_credit_cents is distinct from coalesce(sum(f.credit_cents), 0)
order by t.period
