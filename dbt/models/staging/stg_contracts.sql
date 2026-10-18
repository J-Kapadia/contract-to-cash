select
    contract_id,
    version::integer as version,
    crm_account_id,
    signed_on::date as signed_on,
    effective_from::date as effective_from,
    term_start::date as term_start,
    term_end::date as term_end,
    billing_frequency,
    payment_terms_days::integer as payment_terms_days,
    renewal_number::integer as renewal_number,
    known_on::date as known_on,
    _month,
    _row_hash
from {{ source('raw', 'contracts') }}
