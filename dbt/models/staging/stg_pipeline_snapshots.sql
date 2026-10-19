select
    snapshot_date::date as snapshot_date,
    opp_id,
    crm_account_id,
    opp_type,
    stage,
    acv_cents::bigint as acv_cents,
    term_months::integer as term_months,
    billing_frequency,
    expected_close_date::date as expected_close_date,
    known_on::date as known_on,
    _month,
    _row_hash
from {{ source('raw', 'pipeline_snapshots') }}
