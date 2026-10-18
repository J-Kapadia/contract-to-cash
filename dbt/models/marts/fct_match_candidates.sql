{#- Every blocked pair of customer-name variants with its score and the rules that fired. Weights
    and thresholds come from the policy configuration (`policy()`), reviewed decisions from
    `raw.ref_customer_matches`; both are loaded before dbt runs.

    decision: `auto` at or above auto_match_min; from doubt_min up to auto_match_min the reviewed
    decision (`same` | `different`), NULL while the pair has none; `none` below doubt_min.

    The systems travel with the ids because two systems can spell an id alike (a bank counterparty
    and a ledger name often do).

    A remittance reference only counts between a bank variant and a billing variant: the bank
    transaction's reference text names an invoice the billing customer was billed. -#}
with pairs as (
    select
        c.system_a, c.source_a, c.system_b, c.source_b,
        a.norm_name as name_a, b.norm_name as name_b,
        a.tokens as tokens_a, b.tokens as tokens_b,
        a.domain as domain_a, b.domain as domain_b,
        a.contact_emails as emails_a, b.contact_emails as emails_b
    from {{ ref('int_match_candidates') }} as c
    join {{ ref('int_match_variants') }} as a
        on a.source_system = c.system_a and a.source_id = c.source_a
    join {{ ref('int_match_variants') }} as b
        on b.source_system = c.system_b and b.source_id = c.source_b
),

named_invoices as (
    select counterparty_text as bank_id,
           unnest(regexp_extract_all(lower(reference_text), 'inv_[0-9]+')) as invoice_id
    from {{ source('raw', 'bank_transactions') }}
),

remittances as (
    select distinct n.bank_id, i.billing_customer_id as billing_id
    from named_invoices as n
    join {{ ref('stg_invoices') }} as i using (invoice_id)
),

signals as (
    select
        p.*,
        len(list_intersect(p.emails_a, p.emails_b)) > 0 as email_exact,
        p.domain_a = p.domain_b as domain,
        p.name_a != '' and p.name_a = p.name_b as name_equal,
        len(list_distinct(list_concat(p.tokens_a, p.tokens_b))) > 0
            and len(list_intersect(p.tokens_a, p.tokens_b)) * 10000
                >= {{ policy('jaccard_min_bps') }} * len(list_distinct(list_concat(p.tokens_a, p.tokens_b)))
            as jaccard,
        -- the candidate order puts the bank variant before the billing one
        exists (select 1 from remittances as r
                where p.system_a = 'bank_name' and p.system_b = 'billing'
                  and r.bank_id = p.source_a and r.billing_id = p.source_b) as remittance_ref
    from pairs as p
),

scored as (
    select
        system_a, source_a, system_b, source_b,
        list_filter([
            case when email_exact then 'email_exact' end,
            case when domain then 'domain' end,
            case when name_equal then 'name_equal' end,
            case when jaccard then 'jaccard' end,
            case when remittance_ref then 'remittance_ref' end
        ], rule -> rule is not null) as rules_fired,
        (
            case when email_exact then {{ policy('match_weights.email_exact') }} else 0 end
            + case when domain then {{ policy('match_weights.domain') }} else 0 end
            + case when name_equal then {{ policy('match_weights.name_equal') }} else 0 end
            + case when jaccard then {{ policy('match_weights.jaccard') }} else 0 end
            + case when remittance_ref then {{ policy('match_weights.remittance_ref') }} else 0 end
        )::integer as score
    from signals
)

select
    s.system_a,
    s.source_a,
    s.system_b,
    s.source_b,
    s.score,
    s.rules_fired,
    case
        when s.score >= {{ policy('auto_match_min') }} then 'auto'
        when s.score >= {{ policy('doubt_min') }} then r.decision
        else 'none'
    end as decision
from scored as s
left join raw.ref_customer_matches as r
    on list_sort([r.a, r.b]) = list_sort([s.source_a, s.source_b])
order by s.system_a, s.source_a, s.system_b, s.source_b
