select
    contract_id,
    version::integer as version,
    line_id,
    product,
    annual_fee_cents::bigint as annual_fee_cents,
    included_units::bigint as included_units,
    overage_cents_per_unit::bigint as overage_cents_per_unit,
    known_on::date as known_on,
    _month,
    _row_hash
from {{ source('raw', 'contract_lines') }}
