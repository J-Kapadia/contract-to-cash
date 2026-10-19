-- remediation: Month {month} is locked and its inputs changed: restore them, and post any correction as an entry in the open month.
-- stage: raw
-- A locked month's loaded rows hash to the digest recorded when it was locked. Reports each locked
-- month, up to the one being closed, whose recomputed digest differs from the stored one.

with recomputed as (
    select
        locked.month,
        locked.input_digest as stored_digest,
        sha256(coalesce(string_agg(rows.line, chr(10) order by rows.line), '')) as recomputed_digest
    from state.close_state as locked
    left join state.raw_rows as rows on rows.month = locked.month
    where locked.status = 'locked' and locked.month <= $month
    group by locked.month, locked.input_digest
)

select month, stored_digest, recomputed_digest
from recomputed
where stored_digest is distinct from recomputed_digest
order by month
