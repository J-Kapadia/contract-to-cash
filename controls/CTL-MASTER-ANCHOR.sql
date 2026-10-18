-- remediation: Cluster {members} has {crm_accounts} CRM accounts: add the missing CRM account to the books, or record the pair as different in config/customer_matches.yaml and re-sign.
-- stage: mart
-- Each customer cluster has exactly one CRM account, the key of its master row. A cluster of only
-- bank and ledger names is a payee that is no customer, so it is not a cluster of customers.

select
    list(source_id order by source_system, source_id) as members,
    count(*) filter (where source_system = 'crm') as crm_accounts
from int.int_match_clusters
group by cluster_id
having count(*) filter (where source_system in ('crm', 'billing')) > 0
   and count(*) filter (where source_system = 'crm') <> 1
order by cluster_id
