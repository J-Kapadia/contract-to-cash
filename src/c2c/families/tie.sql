-- The ledger against the receivables sub-ledger per account: the difference, a leaf over the ledger
-- lines of the account and every invoice, payment and applied receipt up to the month.
select
    'tie/' || month || '/' || gl_account as number_id,
    month,
    diff_cents as value_cents,
    null::varchar as value_text,
    'cents' as unit,
    'leaf' as kind,
    'select _row_hash from raw.gl where gl_account = ''' || replace(gl_account, '''', '''''')
        || ''' and gl_period <= ''' || month || ''''
        || ' union all select _row_hash from raw.invoices where _month <= ''' || month || ''''
        || ' union all select _row_hash from raw.payments where _month <= ''' || month || ''''
        || ' union all select banks._row_hash from int.int_cash_application as applied'
        || ' join int.stg_bank_transactions as banks using (bank_txn_id)'
        || ' where applied.kind = ''auto_applied'' and strftime(applied.applied_on, ''%Y-%m'') <= ''' || month || ''''
        as source_query,
    null::varchar[] as children,
    'fct_gl_tieout' as step
from mart.fct_gl_tieout
where month = $month
order by number_id
