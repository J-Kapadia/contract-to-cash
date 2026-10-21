{#-
  The weeks in which the cases cross: one row per week and pair of cases where the case that should
  sit higher ends the week with less cash than the one that should sit lower. The expected order of
  ending cash is downside < base < upside, so the pairs are (downside, base), (base, upside) and
  (downside, upside); `gap_cents` is how far the higher case sits below the lower one.

  `driver` is the line whose net cash (in less out), added up from W01 through the week, most favours
  the lower case over the higher one: the line with the largest difference between the two cases.
  The gap is the sum of those differences over the lines, so the driver is the line that explains most
  of it, for example `commissions` when the upside pays more on bookings. A week's own difference is
  not used: a line that pulled cash forward would name itself in later weeks. Ties go to the line
  that sorts first.
-#}
{%- set as_of = "'" ~ var('as_of') ~ "'::date" -%}

with
pairs as (
    select * from (values
        ('downside', 'base'),
        ('base', 'upside'),
        ('downside', 'upside')
    ) as p (lower_case, higher_case)
),

ending as (
    select distinct "case", week, ending_cents
    from {{ ref('fc_cash_13w') }}
    where as_of = {{ as_of }}
),

crossings as (
    select
        p.lower_case,
        p.higher_case,
        lo.week,
        (lo.ending_cents - hi.ending_cents)::bigint as gap_cents
    from pairs p
    inner join ending lo on lo."case" = p.lower_case
    inner join ending hi on hi."case" = p.higher_case and hi.week = lo.week
    where hi.ending_cents < lo.ending_cents
),

net as (
    select
        "case",
        week,
        line,
        sum(sum(case when line in ('open_ar', 'contracted', 'renewal', 'expansion', 'new') then cents else -cents end)) over (
            partition by "case", line order by week rows between unbounded preceding and current row
        )::bigint as net_cents
    from {{ ref('fc_cash_13w') }}
    where as_of = {{ as_of }}
    group by "case", week, line
),

differences as (
    select
        c.lower_case,
        c.higher_case,
        c.week,
        lo.line,
        lo.net_cents - hi.net_cents as favours_lower_cents
    from crossings c
    inner join net lo on lo."case" = c.lower_case and lo.week = c.week
    inner join net hi on hi."case" = c.higher_case and hi.week = c.week and hi.line = lo.line
),

drivers as (
    select lower_case, higher_case, week, line as driver
    from differences
    qualify row_number() over (
        partition by lower_case, higher_case, week order by favours_lower_cents desc, line) = 1
)

select
    {{ as_of }} as as_of,
    c.week,
    c.lower_case,
    c.higher_case,
    c.gap_cents,
    d.driver
from crossings c
inner join drivers d using (lower_case, higher_case, week)
order by c.week, c.lower_case, c.higher_case
