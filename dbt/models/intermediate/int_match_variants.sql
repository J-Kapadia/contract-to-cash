{#- Every customer-facing spelling in the four source systems, normalised once. A domain is the
    CRM website or the bill-to mailbox's domain, and only when it is not a free-mail domain. -#}
with freemail as (
    select lower(trim(domain)) as domain from {{ source('raw', 'ref_freemail_domains') }}
),

crm as (
    select
        'crm' as source_system,
        a.crm_account_id as source_id,
        a.account_name as raw_name,
        regexp_replace(regexp_replace(regexp_replace(a.website_domain, '^[a-z]+://', ''), '^www\.', ''), '[/?#].*$', '') as domain,
        coalesce(
            (select list_sort(list_distinct(list(c.email)))
             from {{ ref('stg_crm_contacts') }} as c
             where c.crm_account_id = a.crm_account_id and c.email is not null),
            []::varchar[]) as contact_emails
    from {{ ref('stg_crm_accounts') }} as a
),

billing as (
    select
        'billing' as source_system,
        billing_customer_id as source_id,
        bill_to_name as raw_name,
        nullif(split_part(bill_to_email, '@', 2), '') as domain,
        case when bill_to_email is null then []::varchar[] else [bill_to_email] end as contact_emails
    from {{ ref('stg_billing_customers') }}
),

bank as (
    select 'bank_name' as source_system, counterparty_text as source_id, counterparty_text as raw_name,
           null::varchar as domain, []::varchar[] as contact_emails
    from {{ ref('stg_bank_counterparties') }}
),

ledger as (
    select 'gl_name' as source_system, subledger_name as source_id, subledger_name as raw_name,
           null::varchar as domain, []::varchar[] as contact_emails
    from {{ ref('stg_gl_subledger_names') }}
),

named as (
    select * from crm
    union all select * from billing
    union all select * from bank
    union all select * from ledger
)

select
    source_system,
    source_id,
    raw_name,
    {{ normalise_name('raw_name') }} as norm_name,
    list_sort(list_distinct(list_filter(
        string_split({{ normalise_name('raw_name') }}, ' '), _token -> _token != ''))) as tokens,
    case when domain in (select domain from freemail) then null else nullif(domain, '') end as domain,
    contact_emails
from named
order by source_system, source_id
