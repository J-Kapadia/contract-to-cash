-- Cash: opening, receipts, disbursements and closing, each a leaf over the bank rows it moves on.
-- The opening-balance row is part of every balance and of no movement.
with cash as (
    select * from mart.fct_cash_rollforward where month = $month
),

metrics as (
    select 'opening' as metric, opening_cents as cents,
        'is_opening_balance or _month < ''' || month || '''' as condition
    from cash
    union all
    select 'receipts', receipts_cents,
        'not is_opening_balance and amount_cents > 0 and _month = ''' || month || ''''
    from cash
    union all
    select 'disbursements', disbursements_cents,
        'not is_opening_balance and amount_cents < 0 and _month = ''' || month || ''''
    from cash
    union all
    select 'closing', closing_cents,
        'is_opening_balance or _month <= ''' || month || ''''
    from cash
)

select
    'cash/' || $month || '/' || metric as number_id,
    $month as month,
    cents as value_cents,
    null::varchar as value_text,
    'cents' as unit,
    'leaf' as kind,
    'select _row_hash from int.stg_bank_transactions where ' || condition as source_query,
    null::varchar[] as children,
    'fct_cash_rollforward' as step
from metrics
order by number_id
