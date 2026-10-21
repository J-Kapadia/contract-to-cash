-- One leaf per number published for the forecast as of $as_of: a week's cash by line and its opening
-- and ending balance, a month's recognised revenue by source, and a case's first breach week. A
-- number's source rows are the books' rows known on or before the as-of date in the tables its line
-- is forecast from; a balance and an alert depend on every line, so on every one of those tables.
-- Parameter: $as_of.
with
every as (
    select ['bank_transactions', 'invoices', 'payments', 'contracts', 'contract_lines', 'pipeline_snapshots',
            'payroll_runs', 'headcount_plan', 'commission_plan', 'ap_bills', 'vendor_commitments'] as tables
),
sources as (
    select name, tables from (values
        ('open_ar', ['invoices', 'payments']),
        ('contracted', ['contracts', 'contract_lines', 'invoices', 'payments']),
        ('renewal', ['contracts', 'contract_lines']),
        ('expansion', ['contracts', 'contract_lines', 'pipeline_snapshots']),
        ('new', ['contracts', 'contract_lines', 'pipeline_snapshots']),
        ('payroll', ['payroll_runs', 'headcount_plan']),
        ('commissions', ['commission_plan', 'contracts', 'contract_lines', 'pipeline_snapshots']),
        ('cloud', ['ap_bills', 'vendor_commitments', 'bank_transactions']),
        ('vendors', ['ap_bills', 'vendor_commitments', 'bank_transactions']),
        ('events', ['ap_bills', 'vendor_commitments', 'bank_transactions']),
        ('rent', ['ap_bills', 'vendor_commitments', 'bank_transactions'])
    ) as t(name, tables)
    union all
    select name, (select tables from every) from (values ('opening'), ('ending'), ('alert')) as t(name)
),
numbers as (
    select 'cash/' || week || '/' || line as tail, "case", cents as value_cents, null::varchar as value_text,
           'cents' as unit, line as source
    from forecast.weekly where as_of = $as_of
    union all
    select 'cash/' || week || '/opening', "case", min(opening_cents), null, 'cents', 'opening'
    from forecast.weekly where as_of = $as_of group by "case", week
    union all
    select 'cash/' || week || '/ending', "case", min(ending_cents), null, 'cents', 'ending'
    from forecast.weekly where as_of = $as_of group by "case", week
    union all
    select 'rev/' || month || '/' || source, "case", recognised_cents, null, 'cents', source
    from forecast.revenue where as_of = $as_of
    union all
    select 'alert/first_breach_week', "case", null, first_breach_week, 'text', 'alert'
    from forecast.alerts where as_of = $as_of
)
select
    'fc/' || $as_of::varchar || '/' || n."case" || '/' || n.tail as number_id,
    strftime($as_of, '%Y-%m') as month,
    n.value_cents,
    n.value_text,
    n.unit,
    array_to_string(list_transform(s.tables, t ->
        'select _row_hash from raw.' || t || ' where known_on::date <= ''' || $as_of::varchar || '''::date'),
        ' union all ') as source_query
from numbers n
inner join sources s on s.name = n.source
order by number_id
