select
    crm_account_id,
    account_name,
    nullif(lower(trim(website_domain)), '') as website_domain,
    owner_rep_id,
    created_on::date as created_on,
    known_on::date as known_on,
    _month,
    _row_hash
from {{ source('raw', 'crm_accounts') }}
