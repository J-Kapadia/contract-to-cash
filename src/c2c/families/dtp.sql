-- Days to pay per customer: the lower median and the 80th percentile, each a leaf whose text says
-- whether the customer's own history was too short and the pooled value stands in for it. The source
-- rows are the customer's invoices and payments up to the month (all customers for the pooled row),
-- plus the bank receipts applied without a payment row; the window is not applied, so a change older
-- than the window also changes the digest.
with months as (
    select $month as month
),

rows as (
    select
        fct_days_to_pay.month,
        fct_days_to_pay.customer_id,
        '''' || replace(fct_days_to_pay.customer_id, '''', '''''') || '''' as q,
        v.metric,
        v.days,
        fct_days_to_pay.fallback
    from mart.fct_days_to_pay
    inner join months using (month)
    cross join lateral (values ('median', fct_days_to_pay.median_days), ('p80', fct_days_to_pay.p80_days)) as v (metric, days)
)

select
    'dtp/' || month || '/' || customer_id || '/' || metric as number_id,
    month,
    days::bigint as value_cents,
    'fallback ' || fallback::varchar as value_text,
    'days' as unit,
    'leaf' as kind,
    'WITH owners AS (SELECT contract_id, min(crm_account_id) AS customer_id FROM raw.contracts GROUP BY contract_id),'
        || ' invoice_owners AS (SELECT invoice_id, min(contract_id) AS contract_id FROM raw.invoices GROUP BY invoice_id)'
        || ' SELECT invoices._row_hash FROM raw.invoices AS invoices JOIN owners USING (contract_id)'
        || ' WHERE invoices._month <= ''' || month || ''''
        || case when customer_id = '*' then '' else ' AND owners.customer_id = ' || q end
        || ' UNION ALL SELECT payments._row_hash FROM raw.payments AS payments'
        || ' JOIN invoice_owners USING (invoice_id) JOIN owners USING (contract_id)'
        || ' WHERE payments._month <= ''' || month || ''''
        || case when customer_id = '*' then '' else ' AND owners.customer_id = ' || q end
        || ' UNION ALL SELECT banks._row_hash FROM int.int_cash_application AS applied'
        || ' JOIN int.stg_bank_transactions AS banks USING (bank_txn_id)'
        || ' WHERE applied.kind = ''auto_applied'' AND strftime(applied.applied_on, ''%Y-%m'') <= ''' || month || ''''
        || case when customer_id = '*' then '' else ' AND applied.customer_id = ' || q end as source_query,
    null::varchar[] as children,
    'fct_days_to_pay' as step
from rows
order by number_id
