{{ config(materialized='incremental') }}
{#- The month totals, counts and scores the steps after the models write: `master/<metric>` rows come
    from the customer-master report. The model only makes the table; a build keeps what is in it,
    and each writer replaces its own rows. -#}
select
    cast(null as varchar) as month,
    cast(null as varchar) as metric,
    cast(null as bigint) as value_cents,
    cast(null as varchar) as value_text
where false
