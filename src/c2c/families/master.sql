-- The customer master's scores and doubt-band counts: each row of the rollup table named `master/<metric>`,
-- a leaf over the reviewed-match file and the source rows of every candidate pair up to the month.
-- The value is carried as stored, the integer in value_cents and the printed form in value_text.
select
    metric as number_id,
    month,
    value_cents,
    value_text,
    case when metric like 'master/pairs\_%' escape '\' then 'count' else 'bps' end as unit,
    'leaf' as kind,
    'select _row_hash from raw.ref_customer_matches'
        || ' union all select _row_hash from raw.crm_accounts where _month <= ''' || month || ''' and crm_account_id in ('
        || 'select source_a from mart.fct_match_candidates where system_a = ''crm'' union select source_b from mart.fct_match_candidates where system_b = ''crm'')'
        || ' union all select _row_hash from raw.billing_customers where _month <= ''' || month || ''' and billing_customer_id in ('
        || 'select source_a from mart.fct_match_candidates where system_a = ''billing'' union select source_b from mart.fct_match_candidates where system_b = ''billing'')'
        || ' union all select _row_hash from raw.bank_transactions where _month <= ''' || month || ''' and counterparty_text in ('
        || 'select source_a from mart.fct_match_candidates where system_a = ''bank_name'' union select source_b from mart.fct_match_candidates where system_b = ''bank_name'')'
        || ' union all select _row_hash from raw.gl where _month <= ''' || month || ''' and subledger_name in ('
        || 'select source_a from mart.fct_match_candidates where system_a = ''gl_name'' union select source_b from mart.fct_match_candidates where system_b = ''gl_name'')'
        as source_query,
    null::varchar[] as children,
    'fct_rollup' as step
from mart.fct_rollup
where month = $month and starts_with(metric, 'master/')
order by number_id
