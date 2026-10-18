{#- Pairs of variants that share a name token, a domain or a contact email: the only pairs worth
    scoring. A pair appears once, ordered by (system, id); two CRM accounts are distinct customers
    by definition, so they are never paired. -#}
with shared as (
    select a.source_system as system_a, a.source_id as source_a,
           b.source_system as system_b, b.source_id as source_b, 'name_token' as block_reason
    from (select source_system, source_id, unnest(tokens) as token from {{ ref('int_match_variants') }}) as a
    join (select source_system, source_id, unnest(tokens) as token from {{ ref('int_match_variants') }}) as b
        on a.token = b.token

    union all
    select a.source_system, a.source_id, b.source_system, b.source_id, 'domain'
    from {{ ref('int_match_variants') }} as a
    join {{ ref('int_match_variants') }} as b on a.domain = b.domain

    union all
    select a.source_system, a.source_id, b.source_system, b.source_id, 'contact_email'
    from (select source_system, source_id, unnest(contact_emails) as email from {{ ref('int_match_variants') }}) as a
    join (select source_system, source_id, unnest(contact_emails) as email from {{ ref('int_match_variants') }}) as b
        on a.email = b.email
)

select
    system_a, source_a, system_b, source_b,
    string_agg(distinct block_reason, ',' order by block_reason) as block_reason
from shared
where (system_a, source_a) < (system_b, source_b)
  and not (system_a = 'crm' and system_b = 'crm')
group by system_a, source_a, system_b, source_b
order by system_a, source_a, system_b, source_b
