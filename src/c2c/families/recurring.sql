-- Recognised revenue of the month split into recurring and non-recurring, and their total: a leaf each,
-- over the invoice lines (and, for the recurring ones, the contract lines they refer to) that contribute to
-- the month. A subscription line is recurring when a contract line with its contract and line id exists;
-- overage lines, credit lines and subscription lines without one are non-recurring.
with split as (
    select
        classification,
        recognised_cents,
        '''' || $month || '''' as m
    from mart.fct_revenue_recurring
    where month = $month
),

queries as (
    select
        classification,
        recognised_cents,
        case classification
            when 'recurring' then
                'select i._row_hash from raw.invoices as i join raw.contract_lines as c'
                    || ' on c.contract_id = i.contract_id and c.line_id = i.contract_line_id'
                    || ' where i.line_type = ''subscription'''
                    || ' and substr(i.service_start, 1, 7) <= ' || m || ' and substr(i.service_end, 1, 7) >= ' || m
                    || ' union all select c._row_hash from raw.invoices as i join raw.contract_lines as c'
                    || ' on c.contract_id = i.contract_id and c.line_id = i.contract_line_id'
                    || ' where i.line_type = ''subscription'''
                    || ' and substr(i.service_start, 1, 7) <= ' || m || ' and substr(i.service_end, 1, 7) >= ' || m
            else
                'select i._row_hash from raw.invoices as i'
                    || ' where (i.line_type = ''overage'' and substr(i.service_start, 1, 7) = ' || m || ')'
                    || ' or (i.line_type = ''credit'' and substr(i.service_start, 1, 7) <= ' || m
                    || ' and substr(i.service_end, 1, 7) >= ' || m || ')'
                    || ' or (i.line_type = ''subscription'' and substr(i.service_start, 1, 7) <= ' || m
                    || ' and substr(i.service_end, 1, 7) >= ' || m || ' and not exists (select 1 from raw.contract_lines as c'
                    || ' where c.contract_id = i.contract_id and c.line_id = i.contract_line_id))'
        end as source_query
    from split
)

select
    'recurring/' || $month || '/' || classification as number_id,
    $month as month,
    recognised_cents as value_cents,
    null::varchar as value_text,
    'cents' as unit,
    'leaf' as kind,
    source_query,
    null::varchar[] as children,
    'fct_revenue_recurring' as step
from queries
union all
select
    'recurring/' || $month || '/total',
    $month,
    sum(recognised_cents)::bigint,
    null::varchar,
    'cents',
    'leaf',
    string_agg(source_query, ' union all ' order by classification),
    null::varchar[],
    'fct_revenue_recurring'
from queries
having count(*) > 0
order by number_id
