-- remediation: Set gl_period of the GL entries of invoice {invoice_id} to {invoice_month}, the month of its invoice date.
-- stage: mart
-- Every GL entry of an invoice sits in the month of the invoice's date. Reports each invoice with
-- an entry in another period, for the months up to the one being closed.

with invoice_months as (
    select
        invoice_id,
        min(contract_id) as contract_id,
        strftime(min(invoice_date), '%Y-%m') as invoice_month
    from int.stg_invoices
    group by invoice_id
),

customer_key as (
    select contract_id, min(crm_account_id) as customer_id
    from int.stg_contracts
    group by contract_id
),

violations as (
    select
        invoice_months.invoice_id,
        invoice_months.invoice_month,
        customer_key.customer_id,
        list(gl._row_hash order by gl._row_hash) as _row_hash
    from invoice_months
    join int.stg_gl as gl on gl.source_ref = invoice_months.invoice_id
    left join customer_key using (contract_id)
    where gl.gl_period <> invoice_months.invoice_month
        and invoice_months.invoice_month <= $month
    group by all
),

numbers as (
    select
        violations.invoice_id,
        list(distinct number_id order by number_id) as number_ids
    from violations
    join (
        select 'rev/' || month || '/' || customer_id || '/' || line_type as number_id, month, customer_id
        from mart.fct_revenue
        union all
        select 'defrev/' || month || '/' || customer_id || '/closing', month, customer_id
        from mart.fct_defrev_rollforward
    ) as published
        on published.month = violations.invoice_month and published.customer_id = violations.customer_id
    group by violations.invoice_id
)

select
    violations.invoice_id,
    violations.invoice_month,
    violations._row_hash,
    coalesce(numbers.number_ids, []) as number_ids
from violations
left join numbers using (invoice_id)
order by violations.invoice_id
