-- Aged receivables: each customer's open balance in a bucket is a leaf, each bucket's total over the
-- customers a composite of those leaves. A leaf's source rows are the customer's invoices and payments
-- up to the month plus the bank receipts applied without a payment row; the bucket does not narrow
-- them, so a change to any invoice of the customer changes every bucket's digest.
with rows as (
    select
        month,
        customer_id,
        '''' || replace(customer_id, '''', '''''') || '''' as q,
        bucket,
        open_cents
    from mart.fct_ar_aging
    where month = $month and customer_id is not null
),

leaves as (
    select
        'aging/' || month || '/' || customer_id || '/' || bucket as number_id,
        month,
        open_cents as value_cents,
        'leaf' as kind,
        'WITH owners AS (SELECT contract_id, min(crm_account_id) AS customer_id FROM raw.contracts GROUP BY contract_id),'
            || ' invoice_owners AS (SELECT invoice_id, min(contract_id) AS contract_id FROM raw.invoices GROUP BY invoice_id)'
            || ' SELECT invoices._row_hash FROM raw.invoices AS invoices JOIN owners USING (contract_id)'
            || ' WHERE owners.customer_id = ' || q || ' AND invoices._month <= ''' || month || ''''
            || ' UNION ALL SELECT payments._row_hash FROM raw.payments AS payments'
            || ' JOIN invoice_owners USING (invoice_id) JOIN owners USING (contract_id)'
            || ' WHERE owners.customer_id = ' || q || ' AND payments._month <= ''' || month || ''''
            || ' UNION ALL SELECT banks._row_hash FROM int.int_cash_application AS applied'
            || ' JOIN int.stg_bank_transactions AS banks USING (bank_txn_id)'
            || ' WHERE applied.kind = ''auto_applied'' AND applied.customer_id = ' || q
            || ' AND strftime(applied.applied_on, ''%Y-%m'') <= ''' || month || '''' as source_query,
        null::varchar[] as children
    from rows
),

composites as (
    select
        'aging/' || month || '/' || bucket as number_id,
        month,
        sum(open_cents)::bigint as value_cents,
        'composite' as kind,
        null::varchar as source_query,
        list('aging/' || month || '/' || customer_id || '/' || bucket order by customer_id) as children
    from rows
    group by month, bucket
)

select number_id, month, value_cents, null::varchar as value_text, 'cents' as unit, kind, source_query, children,
    'fct_ar_aging' as step
from leaves
union all
select number_id, month, value_cents, null::varchar as value_text, 'cents' as unit, kind, source_query, children,
    'fct_ar_aging' as step
from composites
order by number_id
