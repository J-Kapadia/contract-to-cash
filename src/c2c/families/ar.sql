-- Receivables per customer: opening, billed, collected, credits and closing, each a leaf. A balance is
-- the invoice and payment rows of the customer's contracts up to the month it closes; a movement is
-- the rows of the month. Collected also holds the bank receipts applied without a payment row.
with months as (
    select
        $month as month,
        strftime(strptime($month, '%Y-%m') - interval 1 month, '%Y-%m') as previous
),

balances as (
    select
        fct_ar_rollforward.customer_id,
        '''' || replace(fct_ar_rollforward.customer_id, '''', '''''') || '''' as q,
        months.month,
        months.previous,
        fct_ar_rollforward.opening_cents,
        fct_ar_rollforward.billed_cents,
        fct_ar_rollforward.collected_cents,
        fct_ar_rollforward.credits_cents,
        fct_ar_rollforward.closing_cents
    from mart.fct_ar_rollforward
    inner join months using (month)
    where fct_ar_rollforward.customer_id is not null
),

metrics as (
    select customer_id, q, month, 'opening' as metric, opening_cents as cents,
        'invoices._month <= ''' || previous || '''' as invoice_filter,
        'payments._month <= ''' || previous || '''' as payment_filter,
        '' as extra
    from balances
    union all
    select customer_id, q, month, 'billed', billed_cents,
        'invoices._month = ''' || month || ''' and invoices.line_type <> ''credit''',
        'false', ''
    from balances
    union all
    select customer_id, q, month, 'collected', collected_cents,
        'false',
        'substr(payments.applied_on, 1, 7) = ''' || month || '''',
        ' union all select banks._row_hash from int.int_cash_application as applied'
            || ' join int.stg_bank_transactions as banks using (bank_txn_id)'
            || ' where applied.kind = ''auto_applied'' and applied.customer_id = ' || q
            || ' and strftime(applied.applied_on, ''%Y-%m'') = ''' || month || ''''
    from balances
    union all
    select customer_id, q, month, 'credits', credits_cents,
        'invoices._month = ''' || month || ''' and invoices.line_type = ''credit''',
        'false', ''
    from balances
    union all
    select customer_id, q, month, 'closing', closing_cents,
        'invoices._month <= ''' || month || '''',
        'payments._month <= ''' || month || '''',
        ''
    from balances
)

select
    'ar/' || month || '/' || customer_id || '/' || metric as number_id,
    month,
    cents as value_cents,
    null::varchar as value_text,
    'cents' as unit,
    'leaf' as kind,
    'WITH owners AS (SELECT contract_id, min(crm_account_id) AS customer_id FROM raw.contracts GROUP BY contract_id),'
        || ' invoice_owners AS (SELECT invoice_id, min(contract_id) AS contract_id FROM raw.invoices GROUP BY invoice_id)'
        || ' SELECT invoices._row_hash FROM raw.invoices AS invoices JOIN owners USING (contract_id)'
        || ' WHERE owners.customer_id = ' || q || ' AND ' || invoice_filter
        || ' UNION ALL SELECT payments._row_hash FROM raw.payments AS payments'
        || ' JOIN invoice_owners USING (invoice_id) JOIN owners USING (contract_id)'
        || ' WHERE owners.customer_id = ' || q || ' AND ' || payment_filter
        || extra as source_query,
    null::varchar[] as children,
    'fct_ar_rollforward' as step
from metrics
order by number_id
