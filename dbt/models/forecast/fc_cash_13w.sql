{#-
  The 13 weeks W01..W13 of forecast cash per case and line, from the day after the as-of date in
  7-day buckets, as positive cents (a line is either cash in or cash out, as in `fct_cash_daily`).
  Every case, week and line has a row, with 0 cents where nothing is expected, so the opening and
  ending balance are on every row of a week.

  W01 opens at the closing cash of the as-of date's month in `fct_cash_rollforward`, the bank
  balance the close ties to; each later week opens at the ending of the one before, and
  ending = opening + cash in - cash out.
-#}
{%- set as_of = "'" ~ var('as_of') ~ "'::date" -%}

with
weeks as (
    select
        'W' || lpad(i::varchar, 2, '0') as week,
        ({{ as_of }} + 1 + 7 * (i - 1))::date as week_start,
        ({{ as_of }} + 7 * i)::date as week_end
    from (select unnest(range(1, 14))::int as i)
),

known_lines as (
    select unnest(['open_ar', 'contracted', 'renewal', 'expansion', 'new',
                   'payroll', 'commissions', 'cloud', 'vendors', 'events', 'rent']) as line
),

cases as (
    select distinct "case" from {{ ref('fct_cash_daily') }} where as_of = {{ as_of }}
),

lines as (
    select line from known_lines
    union
    select distinct line from {{ ref('fct_cash_daily') }} where as_of = {{ as_of }}
),

by_line as (
    select
        d."case",
        w.week,
        d.line,
        sum(d.cents)::bigint as cents,
        sum(case d.direction when 'in' then d.cents else -d.cents end)::bigint as net_cents
    from {{ ref('fct_cash_daily') }} d
    inner join weeks w on d.date between w.week_start and w.week_end
    where d.as_of = {{ as_of }}
    group by d."case", w.week, d.line
),

net as (
    select c."case", w.week, coalesce(sum(b.net_cents), 0) as net_cents
    from cases c
    cross join weeks w
    left join by_line b on b."case" = c."case" and b.week = w.week
    group by c."case", w.week
),

opening as (
    select closing_cents as cents
    from {{ ref('fct_cash_rollforward') }}
    where month = strftime({{ as_of }}, '%Y-%m')
),

balances as (
    select
        "case",
        week,
        (select cents from opening) + coalesce(sum(net_cents) over (
            partition by "case" order by week rows between unbounded preceding and 1 preceding), 0) as opening_cents,
        (select cents from opening) + sum(net_cents) over (
            partition by "case" order by week rows between unbounded preceding and current row) as ending_cents
    from net
)

select
    {{ as_of }} as as_of,
    c."case",
    w.week,
    w.week_start,
    w.week_end,
    l.line,
    coalesce(b.cents, 0)::bigint as cents,
    n.opening_cents::bigint as opening_cents,
    n.ending_cents::bigint as ending_cents
from cases c
cross join weeks w
cross join lines l
inner join balances n on n."case" = c."case" and n.week = w.week
left join by_line b on b."case" = c."case" and b.week = w.week and b.line = l.line
