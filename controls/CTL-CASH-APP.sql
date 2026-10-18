-- remediation: Re-point payment {payment_id} to an open invoice of {payer_customer}, or correct bank row {bank_txn_id}.
-- stage: mart
-- A payment is applied to the customer that paid it. Its billing customer, the billing customer of
-- the invoice it is applied to and the bank counterparty of its receipt must all resolve to the
-- same master customer. A counterparty that resolves to no customer is left to CTL-BANK-UNMATCHED.

with billing_members as (
    select source_id as billing_customer_id, customer_id
    from int.int_customer_members
    where source_system = 'billing'
),

bank_members as (
    select source_id as counterparty_text, customer_id
    from int.int_customer_members
    where source_system = 'bank_name'
),

invoices as (
    select invoice_id, min(billing_customer_id) as billing_customer_id
    from int.stg_invoices
    group by invoice_id
)

select
    payments.payment_id,
    payments.bank_txn_id,
    payments.invoice_id,
    payer.customer_id as payer_customer,
    billed.customer_id as invoice_customer,
    bank_members.customer_id as bank_customer,
    [payments._row_hash] as _row_hash
from int.stg_payments as payments
left join billing_members as payer on payer.billing_customer_id = payments.billing_customer_id
left join invoices using (invoice_id)
left join billing_members as billed on billed.billing_customer_id = invoices.billing_customer_id
left join int.stg_bank_transactions as bank using (bank_txn_id)
left join bank_members on bank_members.counterparty_text = bank.counterparty_text
where billed.customer_id is distinct from payer.customer_id
   or (bank_members.customer_id is not null and bank_members.customer_id <> payer.customer_id)
order by payments.payment_id, payments.invoice_id
