-- remediation: GL account {gl_account} ({entry_count} ledger lines, first in {first_period}) has no row in data/reference/gl_accounts.csv: add it with its name and kind, or correct the account on the lines.
-- stage: raw
-- The reference is the account-to-role mapping: every account the models tie, roll forward or report
-- is named there and has a kind. A ledger account with no row, or a row without a name or a kind, is
-- silently left out of everything. The check runs on the loaded books before any model is built.

select
    l.gl_account,
    count(*) as entry_count,
    min(l.gl_period) as first_period,
    list(l._row_hash order by l._row_hash) as _row_hash
from raw.gl as l
left join raw.ref_gl_accounts as r on r.gl_account = l.gl_account
where r.gl_account is null
    or coalesce(trim(r.kind), '') = ''
    or coalesce(trim(r.name), '') = ''
group by l.gl_account
order by l.gl_account
