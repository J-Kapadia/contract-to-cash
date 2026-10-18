{#- Each variant with the customer id of its cluster: the key for joining billing customers, ledger
    names and bank counterparties to the master. customer_id is NULL when the cluster has no CRM
    account or several. -#}
with anchors as (
    select cluster_id, count(*) as crm_accounts, min(source_id) as crm_account_id
    from {{ ref('int_match_clusters') }}
    where source_system = 'crm'
    group by cluster_id
)

select
    m.source_system,
    m.source_id,
    case when a.crm_accounts = 1 then a.crm_account_id end as customer_id
from {{ ref('int_match_clusters') }} as m
left join anchors as a using (cluster_id)
order by m.source_system, m.source_id
