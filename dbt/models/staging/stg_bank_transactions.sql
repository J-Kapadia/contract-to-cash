-- The bank statement, typed. The first row (earliest posting, then lowest id) carries the opening
-- balance rather than a movement and is flagged so the cash roll-forward can start from it.

select
    bank_txn_id,
    posted_on::date as posted_on,
    amount_cents::bigint as amount_cents,
    counterparty_text,
    reference_text,
    known_on::date as known_on,
    row_number() over (order by posted_on::date, bank_txn_id) = 1 as is_opening_balance,
    _month,
    _row_hash
from {{ source('raw', 'bank_transactions') }}
