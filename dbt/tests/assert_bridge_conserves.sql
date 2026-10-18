-- The bridge conserves: each month's net billings equal the signed sum of its invoice lines (a
-- credit line is negative) and its receipts equal the cash roll-forward's receipts. Each returned
-- row is a violation.
--
-- dbt selects a test when any of its parents is selected, so a build of a roll-forward alone reaches
-- this test before fct_bridge exists; with no fct_bridge there is nothing to check. The hints name the
-- parents the conditional refs would otherwise hide from dbt.
-- depends_on: {{ ref('fct_bridge') }}
-- depends_on: {{ ref('stg_invoices') }}
-- depends_on: {{ ref('fct_cash_rollforward') }}
{% if load_relation(ref('fct_bridge')) is none %}
select '' as month, '' as line, 0::bigint as bridge_cents, 0::bigint as source_cents
where false
{% else %}

with billed as (
    select _month as month, sum(amount_cents)::bigint as cents
    from {{ ref('stg_invoices') }}
    group by _month
),

received as (
    select month, receipts_cents::bigint as cents
    from {{ ref('fct_cash_rollforward') }}
),

bridge as (
    select month, line, cents
    from {{ ref('fct_bridge') }}
    where line in ('net_billings', 'receipts')
),

sources as (
    select month, 'net_billings' as line, cents from billed
    union all
    select month, 'receipts' as line, cents from received
)

select
    bridge.month,
    bridge.line,
    bridge.cents as bridge_cents,
    coalesce(sources.cents, 0) as source_cents
from bridge
left join sources using (month, line)
where bridge.cents <> coalesce(sources.cents, 0)
{% endif %}
