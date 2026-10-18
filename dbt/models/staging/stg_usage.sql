select
    contract_id,
    month,
    units::bigint as units,
    known_on::date as known_on,
    _month,
    _row_hash
from {{ source('raw', 'usage_monthly') }}
