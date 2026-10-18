-- Bank receipts applied to invoices, one row per application.
--
-- A payment row applies part or all of a bank receipt to an invoice (a receipt may carry several
-- rows, an invoice several payments). A receipt with no payment row is applied on its own when its
-- counterparty resolves to a customer of the master and its amount equals the outstanding balance
-- of exactly one invoice of that customer dated on or before the receipt; otherwise it is
-- 'unmatched' and applies nothing.
--
-- Where the books are silent: an invoice's outstanding balance is its lines' net total less the
-- payments applied by the receipt's date; two receipts for the same open invoice are not told apart.

with billing_members as (
    select source_id as billing_customer_id, customer_id
    from {{ ref('int_customer_members') }}
    where source_system = 'billing'
),

bank_members as (
    select source_id as counterparty_text, customer_id
    from {{ ref('int_customer_members') }}
    where source_system = 'bank_name'
),

invoices as (
    select
        invoice_id,
        min(billing_customer_id) as billing_customer_id,
        min(invoice_date) as invoice_date,
        sum(amount_cents)::bigint as invoice_cents
    from {{ ref('stg_invoices') }}
    group by invoice_id
),

applied as (
    select
        stg_payments.bank_txn_id,
        stg_payments.payment_id,
        stg_payments.invoice_id,
        billing_members.customer_id,
        stg_payments.applied_cents,
        stg_payments.applied_on,
        'applied' as kind
    from {{ ref('stg_payments') }}
    left join invoices using (invoice_id)
    left join billing_members
        on billing_members.billing_customer_id = coalesce(invoices.billing_customer_id, stg_payments.billing_customer_id)
),

receipts as (
    select
        stg_bank_transactions.bank_txn_id,
        stg_bank_transactions.posted_on,
        stg_bank_transactions.amount_cents,
        bank_members.customer_id
    from {{ ref('stg_bank_transactions') }}
    left join bank_members using (counterparty_text)
    where stg_bank_transactions.amount_cents > 0
      and not stg_bank_transactions.is_opening_balance
      and stg_bank_transactions.bank_txn_id not in (select bank_txn_id from {{ ref('stg_payments') }})
),

exact_matches as (
    select
        receipts.bank_txn_id,
        invoices.invoice_id
    from receipts
    inner join billing_members on billing_members.customer_id = receipts.customer_id
    inner join invoices
        on invoices.billing_customer_id = billing_members.billing_customer_id
        and invoices.invoice_date <= receipts.posted_on
    where invoices.invoice_cents - coalesce((
            select sum(stg_payments.applied_cents)
            from {{ ref('stg_payments') }}
            where stg_payments.invoice_id = invoices.invoice_id
              and stg_payments.applied_on <= receipts.posted_on
        ), 0) = receipts.amount_cents
),

unique_matches as (
    select bank_txn_id, min(invoice_id) as invoice_id
    from exact_matches
    group by bank_txn_id
    having count(distinct invoice_id) = 1
),

unapplied as (
    select
        receipts.bank_txn_id,
        null::varchar as payment_id,
        unique_matches.invoice_id,
        receipts.customer_id,
        case when unique_matches.invoice_id is null then 0 else receipts.amount_cents end::bigint as applied_cents,
        receipts.posted_on as applied_on,
        case when unique_matches.invoice_id is null then 'unmatched' else 'auto_applied' end as kind
    from receipts
    left join unique_matches using (bank_txn_id)
)

select * from applied
union all
select * from unapplied
order by bank_txn_id, payment_id, invoice_id
