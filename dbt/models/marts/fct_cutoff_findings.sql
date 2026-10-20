{#- The invoices whose ledger entries sit in a period other than the month of the invoice date, one row per
    invoice and period: the predicate of CTL-CUTOFF, kept per finding so the memo can name the invoice and
    the periods involved. `month` is the invoice month, the month the finding belongs to.

    A closed month has no finding here: the control stops the close of a month that has one, and the
    models are built before the mart-stage controls run, so the table is a record of what a stopped close
    found.

    amount_cents is the sum of the misplaced lines' positive amounts, the
    debit side of the invoice. -#}

with invoice_months as (
    select
        invoice_id,
        min(contract_id) as contract_id,
        strftime(min(invoice_date), '%Y-%m') as invoice_month
    from {{ ref('stg_invoices') }}
    group by invoice_id
),

customer_key as (
    select contract_id, min(crm_account_id) as customer_id
    from {{ ref('stg_contracts') }}
    group by contract_id
)

select
    invoice_months.invoice_month as month,
    invoice_months.invoice_id,
    invoice_months.invoice_month,
    gl.gl_period,
    customer_key.customer_id,
    count(*)::bigint as entries,
    sum(case when gl.amount_cents > 0 then gl.amount_cents else 0 end)::bigint as amount_cents,
    list(gl.entry_id order by gl.entry_id, gl.line_no) as entry_ids
from invoice_months
inner join {{ ref('stg_gl') }} as gl on gl.source_ref = invoice_months.invoice_id
left join customer_key using (contract_id)
where gl.gl_period <> invoice_months.invoice_month
group by all
