-- Days sales outstanding: the month's closing receivables and billings over every customer, the days
-- of its calendar and, when it billed anything, the days outstanding, each a leaf. The receivables are
-- the invoice and payment rows up to the month plus the bank receipts applied without a payment row;
-- the billings are the month's invoice rows other than credits; the days of the calendar read no rows.
with rows as (
    select month, ar_closing_cents, billed_cents, days_in_month, dso_days
    from mart.fct_dso
    where month = $month
),

sources as (
    select
        month,
        'SELECT invoices._row_hash FROM raw.invoices AS invoices WHERE invoices._month <= ''' || month || ''''
            || ' UNION ALL SELECT payments._row_hash FROM raw.payments AS payments WHERE payments._month <= ''' || month || ''''
            || ' UNION ALL SELECT banks._row_hash FROM int.int_cash_application AS applied'
            || ' JOIN int.stg_bank_transactions AS banks USING (bank_txn_id)'
            || ' WHERE applied.kind = ''auto_applied'' AND strftime(applied.applied_on, ''%Y-%m'') <= ''' || month || '''' as closing,
        'SELECT invoices._row_hash FROM raw.invoices AS invoices WHERE invoices._month = ''' || month || ''''
            || ' AND invoices.line_type <> ''credit''' as billed
    from rows
),

leaves as (
    select 'ar_closing' as metric, month, ar_closing_cents as value, 'cents' as unit, closing as source_query
    from rows inner join sources using (month)
    union all
    select 'billed', month, billed_cents, 'cents', billed
    from rows inner join sources using (month)
    union all
    select 'days_in_month', month, days_in_month, 'days', 'select _row_hash from raw.gl where false'
    from rows
    union all
    select 'days', month, dso_days, 'days', closing || ' UNION ALL ' || billed
    from rows inner join sources using (month)
    where dso_days is not null
)

select
    'dso/' || month || '/' || metric as number_id,
    month,
    value::bigint as value_cents,
    null::varchar as value_text,
    unit,
    'leaf' as kind,
    source_query,
    null::varchar[] as children,
    'fct_dso' as step
from leaves
order by number_id
