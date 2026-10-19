{{ config(materialized='incremental', incremental_strategy='delete+insert', unique_key=['month', 'metric']) }}
{#- The month totals, counts and scores the memo and the reports print. The model writes `stale_opps`,
    the open new and expansion opportunities of each month-end snapshot whose expected close date is
    before the snapshot date; the rows other steps write (`master/<metric>`) are kept across builds,
    and each writer replaces its own rows. The customer-master report builds this model without its
    staging parents, so the count is written only where the staged snapshots have been built. -#}
{%- set snapshots = ref('stg_pipeline_snapshots') -%}
{%- set staged = execute and adapter.get_relation(database=snapshots.database, schema=snapshots.schema, identifier=snapshots.identifier) is not none -%}
{%- set is_stale -%}
    stage in ('S1', 'S2', 'S3', 'S4', 'S5')
    and opp_type in ('new', 'expansion')
    and expected_close_date < snapshot_date
{%- endset %}

{% if staged -%}
select
    strftime(snapshot_date, '%Y-%m') as month,
    'stale_opps' as metric,
    count(*) filter (where {{ is_stale }})::bigint as value_cents,
    count(*) filter (where {{ is_stale }}) || ' opps' as value_text
from {{ snapshots }}
group by snapshot_date
{%- else -%}
select
    cast(null as varchar) as month,
    cast(null as varchar) as metric,
    cast(null as bigint) as value_cents,
    cast(null as varchar) as value_text
where false
{%- endif %}
