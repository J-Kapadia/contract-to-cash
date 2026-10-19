-- remediation: The roll-forward differs from GL {gl_account} by {diff_cents} cents in {month}: review GL entries {entry_ids}.
-- stage: mart
-- Recognised revenue against GL 4000 and 4100. Revenue is period activity, so every month up to the one
-- being closed is checked on its own: a difference is reported in the month it arises and not carried.

with violations as (
    select month, gl_account, diff_cents
    from mart.fct_gl_tieout
    where gl_account in ('4000', '4100') and diff_cents <> 0 and month <= $month
),

entries as (
    select
        violations.month,
        violations.gl_account,
        list(distinct gl.entry_id order by gl.entry_id) as entry_ids,
        list(distinct gl._row_hash order by gl._row_hash) as row_hashes
    from violations
    join raw.gl as gl
        on gl.gl_period = violations.month and gl.gl_account = violations.gl_account
    group by violations.month, violations.gl_account
),

numbers as (
    select
        violations.month,
        violations.gl_account,
        coalesce(
            list('rev/' || violations.month || '/' || revenue.customer_id || '/' || revenue.line_type
                 order by revenue.customer_id, revenue.line_type) filter (where revenue.customer_id is not null),
            []
        ) as number_ids
    from violations
    left join mart.fct_revenue as revenue
        on revenue.month = violations.month
        and (case when revenue.line_type = 'overage' then '4100' else '4000' end) = violations.gl_account
    group by violations.month, violations.gl_account
)

select
    violations.month,
    violations.gl_account,
    violations.diff_cents,
    coalesce(entries.entry_ids, []) as entry_ids,
    coalesce(entries.row_hashes, []) as _row_hash,
    numbers.number_ids
from violations
left join entries using (month, gl_account)
left join numbers using (month, gl_account)
order by violations.month, violations.gl_account
