select distinct subledger_name
from {{ ref('stg_gl') }}
where nullif(trim(subledger_name), '') is not null
