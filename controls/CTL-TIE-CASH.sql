-- remediation: The roll-forward differs from GL {gl_account} by {diff_cents} cents in {month}: review GL entries {entry_ids}.
-- stage: mart
-- Cash against GL 1000 (the bank balance). Reports the month the difference first appears (or changes),
-- for every month up to the one being closed; later months only carry it forward.

with tie as (
    select
        month,
        gl_account,
        diff_cents,
        diff_cents - coalesce(lag(diff_cents) over (partition by gl_account order by month), 0) as change_cents
    from mart.fct_gl_tieout
    where gl_account = '1000'
),

violations as (
    select month, gl_account, diff_cents
    from tie
    where diff_cents <> 0 and change_cents <> 0 and month <= $month
),

entries as (
    select
        violations.month,
        list(distinct gl.entry_id order by gl.entry_id) as entry_ids,
        list(distinct gl._row_hash order by gl._row_hash) as row_hashes
    from violations
    join raw.gl as gl
        on gl.gl_period = violations.month and gl.gl_account = violations.gl_account
    group by violations.month
)

select
    violations.month,
    violations.gl_account,
    violations.diff_cents,
    coalesce(entries.entry_ids, []) as entry_ids,
    coalesce(entries.row_hashes, []) as _row_hash,
    ['cash/' || violations.month || '/closing'] as number_ids
from violations
left join entries using (month)
order by violations.month
