select
    billing_customer_id,
    bill_to_name,
    nullif(lower(trim(bill_to_email)), '') as bill_to_email,
    nullif(trim(crm_account_ref), '') as crm_account_ref,
    known_on::date as known_on,
    _month,
    _row_hash
from {{ source('raw', 'billing_customers') }}
