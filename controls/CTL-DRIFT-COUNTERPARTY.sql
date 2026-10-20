-- remediation: Bank counterparty {counterparty_text} ({transactions} statement lines in {month}, first {first_bank_txn_id}) was not in the books through {baseline_month}, the latest month closed before it: confirm who it is and add it to data/reference/counterparties.csv, or apply its receipts to the customer's invoices in payments.csv.
-- stage: mart
-- A bank counterparty of the month being closed that the counterparties reference file does not list is drift when no row dated in or
-- before the latest month closed before it names it: the same text on a statement line, the customer its name clusters to (account, contracts,
-- billing customers, ledger names), or a bill, commitment or ledger sub-ledger of that name. History rows before the books' window count, a
-- receipt of this month does not vouch for its own payer through the invoice it applies to, and the first month of a warehouse has no baseline and passes.

with baseline as (
    select max(month) as month
    from state.close_state
    where status in ('closed', 'locked') and month < $month
),

candidates as (
    select
        counterparty_text,
        count(*) as transactions,
        first(bank_txn_id order by posted_on, bank_txn_id) as first_bank_txn_id,
        list(_row_hash order by posted_on, bank_txn_id) as _row_hash
    from int.stg_bank_transactions
    where _month = $month
        and not is_opening_balance
        and nullif(trim(counterparty_text), '') is not null
        and not exists (
            select 1 from raw.ref_counterparties as listed
            where listed.counterparty_text = stg_bank_transactions.counterparty_text
        )
    group by counterparty_text
),

clustered as (
    select source_id as counterparty_text, customer_id
    from int.int_customer_members
    where source_system = 'bank_name' and customer_id is not null
),

customer_months as (
    select crm_account_id as customer_id, _month as month from raw.crm_accounts
    union
    select crm_account_id, _month from raw.contracts
    union
    select members.customer_id, billing._month
    from int.int_customer_members as members
    join raw.billing_customers as billing on billing.billing_customer_id = members.source_id
    where members.source_system = 'billing' and members.customer_id is not null
    union
    select members.customer_id, gl._month
    from int.int_customer_members as members
    join raw.gl as gl on gl.subledger_name = members.source_id
    where members.source_system = 'gl_name' and members.customer_id is not null
),

appearances as (
    select counterparty_text, _month as month from raw.bank_transactions
    union
    select clustered.counterparty_text, customer_months.month
    from clustered
    join customer_months using (customer_id)
    union
    select lower(trim(vendor_name)), _month from raw.ap_bills
    union
    select lower(trim(vendor_name)), _month from raw.vendor_commitments
    union
    select lower(trim(subledger_name)), _month from raw.gl
),

known as (
    select candidates.counterparty_text
    from candidates
    join appearances
        on appearances.counterparty_text = candidates.counterparty_text
        or appearances.counterparty_text = lower(trim(candidates.counterparty_text))
    where appearances.month <= (select month from baseline)
    group by candidates.counterparty_text
)

select
    candidates.counterparty_text,
    $month as month,
    baseline.month as baseline_month,
    candidates.transactions,
    candidates.first_bank_txn_id,
    candidates._row_hash
from candidates
cross join baseline
where baseline.month is not null
    and candidates.counterparty_text not in (select counterparty_text from known)
order by candidates.counterparty_text
