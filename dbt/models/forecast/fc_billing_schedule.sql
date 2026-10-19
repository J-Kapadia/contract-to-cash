{#-
  The billing schedule a forecast as of A recognises revenue from, per case: the forecast rows of
  `int_billing_schedule` (renewal, new and expansion contracts) and the `contracted` rows, the
  scheduled invoices of the contracts in force at A. A contract is in force when the contract rows
  known on or before A (`pit_contracts`) have its latest version ending on or after A. The
  schedule is the same for every case, so each contracted row is repeated per case.

  Where the books are silent, overage is left out of the contracted rows (usage is not scheduled,
  and the usage of months after A is not known at A), and an amendment is scheduled as the books
  hold it, whenever it became known.
-#}
{%- set as_of = "'" ~ var('as_of') ~ "'::date" -%}

with
in_force as (
    select contract_id
    from {{ ref('pit_contracts') }}
    qualify row_number() over (partition by contract_id order by version::int desc) = 1
        and term_end::date >= {{ as_of }}
),

cases as (
    select distinct "case" from fc_build.inputs where as_of = {{ as_of }}
),

scheduled as (
    select s.customer_id, s.contract_id, s.line_id, s.invoice_date, s.service_start, s.service_end, s.amount_cents
    from {{ ref('int_billing_schedule') }} s
    inner join in_force using (contract_id)
    where s.source = 'actual'
        and not exists (
            select 1 from {{ ref('pit_contract_lines') }} l
            where l.contract_id = s.contract_id and l.line_id = s.line_id and l.product = 'overage'
        )
)

select
    'contracted' as source,
    {{ as_of }} as as_of,
    cases."case",
    scheduled.customer_id,
    scheduled.contract_id,
    scheduled.line_id,
    scheduled.invoice_date,
    scheduled.service_start,
    scheduled.service_end,
    scheduled.amount_cents
from scheduled
cross join cases

union all

select source, as_of, "case", customer_id, contract_id, line_id, invoice_date, service_start, service_end, amount_cents
from {{ ref('int_billing_schedule') }}
where source <> 'actual' and as_of = {{ as_of }}
