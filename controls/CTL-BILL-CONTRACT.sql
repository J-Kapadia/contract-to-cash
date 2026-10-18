-- remediation: Issue the missing invoice for {contract_id} line {line_id}, service {service_start} to {service_end}, {expected_cents} cents, or correct the contract version.
-- stage: mart
-- The billing schedule of the contracts against the invoices issued, by contract line and service
-- period, to the cent. Both sides cover the invoice dates up to the end of the month being closed.
-- Reports a scheduled invoice that was not issued, one issued for a different amount, and one
-- issued that the contracts do not call for (expected_cents is then 0).

with scheduled as (
    select
        contract_id,
        line_id,
        service_start,
        service_end,
        sum(amount_cents)::bigint as expected_cents
    from mart.fct_billing_schedule
    where source = 'actual' and invoice_date <= last_day(($month || '-01')::date)
    group by all
),

issued as (
    select
        contract_id,
        contract_line_id as line_id,
        service_start,
        service_end,
        sum(amount_cents)::bigint as invoiced_cents,
        list(_row_hash order by _row_hash) as _row_hash
    from int.stg_invoices
    where invoice_date <= last_day(($month || '-01')::date)
    group by all
)

select
    contract_id,
    line_id,
    service_start,
    service_end,
    coalesce(scheduled.expected_cents, 0) as expected_cents,
    coalesce(issued.invoiced_cents, 0) as invoiced_cents,
    coalesce(issued._row_hash, []) as _row_hash
from scheduled
full outer join issued using (contract_id, line_id, service_start, service_end)
where coalesce(scheduled.expected_cents, 0) <> coalesce(issued.invoiced_cents, 0)
order by contract_id, line_id, service_start, service_end
