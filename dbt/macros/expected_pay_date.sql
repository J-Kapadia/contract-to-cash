{#-
  The single definition of an expected cash date.

  expected_pay_date(invoice_date_col, due_date_col, as_of_col, dtp_days_col, lateness_col, calendar_relation)
  is the date an open invoice is expected to be paid, as of the date `as_of_col`:

    1. the invoice date plus `dtp_days_col` days (the case's days to pay); with no days to pay the
       terms stand in, the days from the invoice date to `due_date_col`;
    2. a date that is not after the as-of date is already past, so it becomes the day after the
       as-of date plus `lateness_col` days (the customer's median lateness beyond due; none when
       it is NULL), and never later than 91 days after the as-of date;
    3. the result moves to the next business day on or after it, from `calendar_relation` (a
       relation with a `date` column and an `is_business_day` column of 0 or 1, for example
       `source('raw', 'ref_business_days')`). If that moves a shifted date past 91 days after
       the as-of date, the last business day on or before that day is used instead.

  A date with no calendar row to move to stays as it is. Arguments are SQL expressions and are
  repeated in the output, so pass columns rather than anything expensive. Every model that
  dates expected cash, actual or forecast, calls this macro; no model adds days to an invoice
  date itself. expected_terms_days() is the terms the macro falls back on, for a model that
  wants an expected date no later than the terms.
-#}

{% macro expected_terms_days(invoice_date_col, due_date_col) -%}
date_diff('day', ({{ invoice_date_col }})::date, ({{ due_date_col }})::date)
{%- endmacro %}

{% macro expected_pay_date(invoice_date_col, due_date_col, as_of_col, dtp_days_col, lateness_col, calendar_relation) -%}
{%- set as_of -%}({{ as_of_col }})::date{%- endset -%}
{%- set nominal -%}(({{ invoice_date_col }})::date + coalesce(({{ dtp_days_col }})::int, ({{ expected_terms_days(invoice_date_col, due_date_col) }})::int))::date{%- endset -%}
{%- set last_day_allowed -%}({{ as_of }} + 91){%- endset -%}
{%- set wanted -%}
    case
        when {{ nominal }} > {{ as_of }} then {{ nominal }}
        else least({{ as_of }} + 1 + coalesce(({{ lateness_col }})::int, 0), {{ last_day_allowed }})
    end
{%- endset -%}
{%- set next_business_day -%}
    coalesce(
        (select min(cal."date"::date) from {{ calendar_relation }} as cal
            where cal."date"::date >= {{ wanted }} and cal.is_business_day::int = 1),
        {{ wanted }})
{%- endset -%}
{%- set last_business_day -%}
    coalesce(
        (select max(cal."date"::date) from {{ calendar_relation }} as cal
            where cal."date"::date <= {{ last_day_allowed }} and cal.is_business_day::int = 1),
        {{ last_day_allowed }})
{%- endset -%}
(
    case
        when {{ nominal }} > {{ as_of }} or {{ next_business_day }} <= {{ last_day_allowed }} then {{ next_business_day }}
        else {{ last_business_day }}
    end
)::date
{%- endmacro %}
