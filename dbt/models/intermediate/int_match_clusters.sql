{#- The customer clusters: the connected components of the pairs that merge, either by score or by a
    reviewed `same`. A variant is keyed by its system and id; each key takes the smallest key
    reachable through merged pairs, which depends on the pairs and not on their order. The
    recursion adds a (variant, reachable key) row until none is new, so it ends.

    A cluster is named by its smallest member key. -#}
with recursive nodes as (
    select source_system, source_id, source_system || chr(31) || source_id as node_key
    from {{ ref('int_match_variants') }}
),

edges as (
    select system_a || chr(31) || source_a as node_a, system_b || chr(31) || source_b as node_b
    from {{ ref('fct_match_candidates') }}
    where decision in ('auto', 'same')
),

both_ways as (
    select node_a as from_key, node_b as to_key from edges
    union
    select node_b, node_a from edges
),

reach(node_key, reached_key) as (
    select node_key, node_key from nodes
    union
    select r.node_key, w.to_key
    from reach as r
    join both_ways as w on w.from_key = r.reached_key
),

smallest as (
    select node_key, min(reached_key) as cluster_id from reach group by node_key
)

select n.source_system, n.source_id, s.cluster_id
from nodes as n
join smallest as s using (node_key)
order by s.cluster_id, n.source_system, n.source_id
