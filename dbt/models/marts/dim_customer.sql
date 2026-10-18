{#- One row per cluster of customer-name variants that has a CRM account or a billing customer; a
    cluster of only bank and ledger names is a payee that is no customer (a vendor), not a master
    row. customer_id is the cluster's CRM account id, NULL
    when the cluster has no CRM account or has several; a control reports those, and no id is ever
    made up. display_name is that CRM account's name. review_state is `pending` while a member
    pair in the doubt band has no reviewed decision, `reviewed` when the cluster stands on a
    reviewed `same`, and `auto` otherwise. -#}
with members as (
    select m.cluster_id, m.source_system, m.source_id
    from {{ ref('int_match_clusters') }} as m
),

crm as (
    select m.cluster_id, count(*) as crm_accounts, min(m.source_id) as crm_account_id
    from members as m
    where m.source_system = 'crm'
    group by m.cluster_id
),

pairs as (
    select c.cluster_id, p.decision
    from {{ ref('fct_match_candidates') }} as p
    join {{ ref('int_match_clusters') }} as c
        on (c.source_system, c.source_id) in ((p.system_a, p.source_a), (p.system_b, p.source_b))
    where p.score >= {{ policy('doubt_min') }}
),

review as (
    select
        cluster_id,
        bool_or(decision is null) as pending,
        bool_or(decision = 'same') as reviewed
    from pairs
    group by cluster_id
),

sizes as (
    select cluster_id, count(*) as member_count
    from members
    group by cluster_id
    having count(*) filter (where source_system in ('crm', 'billing')) > 0
)

select
    case when coalesce(crm.crm_accounts, 0) = 1 then crm.crm_account_id end as customer_id,
    case when coalesce(crm.crm_accounts, 0) = 1 then a.account_name end as display_name,
    sizes.member_count::integer as member_count,
    case
        when coalesce(review.pending, false) then 'pending'
        when coalesce(review.reviewed, false) then 'reviewed'
        else 'auto'
    end as review_state
from sizes
left join crm using (cluster_id)
left join review using (cluster_id)
left join {{ ref('stg_crm_accounts') }} as a on a.crm_account_id = crm.crm_account_id
order by sizes.cluster_id
