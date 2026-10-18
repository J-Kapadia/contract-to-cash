select
    entry_id,
    line_no::integer as line_no,
    posted_on::date as posted_on,
    gl_period,
    gl_account,
    amount_cents::bigint as amount_cents,
    subledger_name,
    source_ref,
    known_on::date as known_on,
    _month,
    _row_hash
from {{ source('raw', 'gl') }}
