select distinct counterparty_text
from {{ source('raw', 'bank_transactions') }}
where nullif(trim(counterparty_text), '') is not null
