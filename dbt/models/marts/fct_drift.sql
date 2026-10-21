{#- What changed in the books, per month: the ledger accounts, customers and contract line types seen for
    the first time, and the source files whose row count moved against the previous calendar month by at
    least `drift_volume_shift_bps`. One row per month, category and key over every month the raw books
    hold. A close always closes the previous month first, so the previous calendar month is the prior
    closed month; the first month has no volume rows.

    Where the plan is silent: a table with no row in a month counts zero for it, so a file that goes
    silent is a shift of -10000 bps, and the shift is truncated toward zero.

    With a stacked trial balance in the warehouse (`mart.fct_trial_balance`, written by `c2c load-tb`),
    an account whose first trial balance period is the month and that the raw ledger does not hold in
    that month or before is new too: one `new_account` row with the account's trial balance rows in the
    period as its value. A warehouse without the trial balance gets the rows the ledger alone gives. -#}
{%- set has_tb = execute and adapter.get_relation(database=target.database, schema='mart', identifier='fct_trial_balance') is not none -%}
{% set stems = [
    'crm_accounts', 'crm_contacts', 'pipeline_snapshots', 'contracts', 'contract_lines', 'usage_monthly',
    'billing_customers', 'invoices', 'payments', 'bank_transactions', 'gl', 'payroll_runs',
    'headcount_plan', 'commission_plan', 'ap_bills', 'vendor_commitments'
] %}

with row_months as (
{%- for stem in stems %}
    select '{{ stem }}' as stem, _month as month from {{ source('raw', stem) }}
    {{- '\n    union all' if not loop.last }}
{%- endfor %}
),

stems as (
{%- for stem in stems %}
    select '{{ stem }}' as stem
    {{- '\n    union all' if not loop.last }}
{%- endfor %}
),

spine as (
    select distinct month from row_months where month is not null
),

first_account as (
    select gl_account as key, min(_month) as month
    from {{ source('raw', 'gl') }}
    where gl_account is not null
    group by gl_account
),

first_customer as (
    select crm_account_id as key, min(_month) as month
    from {{ source('raw', 'crm_accounts') }}
    where crm_account_id is not null
    group by crm_account_id
),

first_line_type as (
    select product as key, min(_month) as month
    from {{ source('raw', 'contract_lines') }}
    where product is not null
    group by product
),

new_account as (
    select first_account.month, 'new_account' as category, first_account.key,
        count(*)::bigint as value_cents
    from first_account
    join {{ source('raw', 'gl') }} as lines
        on lines.gl_account = first_account.key and lines._month = first_account.month
    group by first_account.month, first_account.key
),

{%- if has_tb %}

first_tb_account as (
    select account_id as key, min(period) as month
    from mart.fct_trial_balance
    where account_id is not null
    group by account_id
),

new_tb_account as (
    select first_tb_account.month, 'new_account' as category, first_tb_account.key,
        count(*)::bigint as value_cents
    from first_tb_account
    join mart.fct_trial_balance as lines
        on lines.account_id = first_tb_account.key and lines.period = first_tb_account.month
    where not exists (
        select 1 from {{ source('raw', 'gl') }} as ledger
        where ledger.gl_account = first_tb_account.key and ledger._month <= first_tb_account.month
    )
    group by first_tb_account.month, first_tb_account.key
),
{%- endif %}

new_customer as (
    select month, 'new_customer' as category, key, 1::bigint as value_cents
    from first_customer
),

new_line_type as (
    select first_line_type.month, 'new_line_type' as category, first_line_type.key,
        count(*)::bigint as value_cents
    from first_line_type
    join {{ source('raw', 'contract_lines') }} as lines
        on lines.product = first_line_type.key and lines._month = first_line_type.month
    group by first_line_type.month, first_line_type.key
),

row_counts as (
    select stem, month, count(*)::bigint as total
    from row_months
    where month is not null
    group by stem, month
),

shifts as (
    select
        spine.month,
        stems.stem,
        coalesce(this.total, 0) as total,
        coalesce(before.total, 0) as prior
    from spine
    cross join stems
    left join row_counts as this on this.stem = stems.stem and this.month = spine.month
    left join row_counts as before
        on before.stem = stems.stem
        and before.month = strftime(strptime(spine.month || '-01', '%Y-%m-%d') - interval 1 month, '%Y-%m')
),

volume_shift as (
    select
        month,
        'volume_shift' as category,
        stem as key,
        total::bigint as value_cents,
        prior::bigint as prior_cents,
        ((total - prior) * 10000 // prior)::bigint as shift_bps
    from shifts
    where prior > 0
        and abs((total - prior) * 10000 // prior) >= {{ policy('drift_volume_shift_bps') }}
),

firsts as (
    select month, category, key, value_cents from new_account
    {%- if has_tb %}
    union all
    select month, category, key, value_cents from new_tb_account
    {%- endif %}
    union all
    select month, category, key, value_cents from new_customer
    union all
    select month, category, key, value_cents from new_line_type
)

select month, category, key, value_cents, null::bigint as prior_cents, null::bigint as shift_bps
from firsts
union all
select month, category, key, value_cents, prior_cents, shift_bps
from volume_shift
order by month, category, key
