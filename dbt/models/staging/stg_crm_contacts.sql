select
    contact_id,
    crm_account_id,
    nullif(lower(trim(email)), '') as email,
    role,
    known_on::date as known_on,
    _month,
    _row_hash
from {{ source('raw', 'crm_contacts') }}
