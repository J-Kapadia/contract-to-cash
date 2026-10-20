{#- The one-time items of each month, one row per item: a credit memo is an invoice line of type credit; a
    write-off is a ledger line on receivables (1200) that is neither an invoice's nor a cash receipt's, which
    is to say its entry has no line on cash (1000) and its source_ref is no invoice; a manual adjustment is a
    ledger entry that refers to no invoice, payment, bank transaction or recognition and has no line on
    receivables (such an entry is a write-off, counted once).

    ref_id names the item: invoice_id/line_no, entry_id/line_no, or entry_id. amount_cents is the line as
    it is carried; for a manual adjustment, the sum of the entry's positive lines, the debit side. -#}

with credit_memos as (
    select
        _month as month,
        'credit_memo' as category,
        invoice_id || '/' || line_no as ref_id,
        amount_cents
    from {{ ref('stg_invoices') }}
    where line_type = 'credit'
),

entries_on_cash as (
    select distinct entry_id from {{ ref('stg_gl') }} where gl_account = '1000'
),

write_offs as (
    select
        gl.gl_period as month,
        'write_off' as category,
        gl.entry_id || '/' || gl.line_no as ref_id,
        gl.amount_cents
    from {{ ref('stg_gl') }} as gl
    where gl.gl_account = '1200'
        and gl.entry_id not in (select entry_id from entries_on_cash)
        and coalesce(gl.source_ref, '') not in (select invoice_id from {{ ref('stg_invoices') }})
),

entries_on_receivables as (
    select distinct entry_id from {{ ref('stg_gl') }} where gl_account = '1200'
),

references_known as (
    select invoice_id as ref from {{ ref('stg_invoices') }}
    union
    select payment_id from {{ ref('stg_payments') }}
    union
    select bank_txn_id from {{ ref('stg_bank_transactions') }}
),

manual_adjustments as (
    select
        min(gl_period) as month,
        'manual_adjustment' as category,
        entry_id as ref_id,
        sum(case when amount_cents > 0 then amount_cents else 0 end)::bigint as amount_cents
    from {{ ref('stg_gl') }}
    where entry_id not in (select entry_id from entries_on_receivables)
    group by entry_id
    having not bool_or(
        coalesce(source_ref, '') in (select ref from references_known)
        or coalesce(source_ref, '') like 'rec:%'
    )
)

select month, category, ref_id, amount_cents::bigint as amount_cents from credit_memos
union all
select month, category, ref_id, amount_cents::bigint from write_offs
union all
select month, category, ref_id, amount_cents from manual_adjustments
