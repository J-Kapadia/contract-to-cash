{#-
  The single definition of ratable revenue.

  ratable_cents(amount_col, start_col, end_col, month_col) is the part of a line's amount
  recognised in one month, as BIGINT cents. The service period is inclusive of both ends and
  recognition is by day: the cumulative amount through day d of n is amount * d // n (floor
  division), and a month's share is the cumulative at its last day less the cumulative at the day
  before its first. The months of a period therefore sum to the amount exactly, whatever the
  grouping, and no remainder step is needed.

  month_col is a 'YYYY-MM' text value. A month outside the period gets 0. A period that ends
  before it starts raises an error. Arguments are SQL expressions and are repeated in the
  output, so pass columns rather than anything expensive or non-deterministic.

  Every model that recognises revenue, actual or forecast, calls this macro; no model does its own
  proration arithmetic.
-#}

{#- Floor division for a positive divisor; DuckDB's // truncates toward zero. -#}
{% macro c2c_floor_div(numerator, divisor) -%}
(({{ numerator }}) - ((({{ numerator }}) % ({{ divisor }})) + ({{ divisor }})) % ({{ divisor }})) // ({{ divisor }})
{%- endmacro %}

{#- The amount recognised through the end of the day `through`. -#}
{% macro c2c_cumulative_cents(amount_col, start_col, end_col, through) -%}
{%- set days -%}(date_diff('day', ({{ start_col }})::date, ({{ end_col }})::date) + 1){%- endset -%}
{%- set elapsed -%}least(greatest(date_diff('day', ({{ start_col }})::date, {{ through }}) + 1, 0), {{ days }}){%- endset -%}
{{ c2c_floor_div("(" ~ amount_col ~ ")::hugeint * " ~ elapsed, days) }}
{%- endmacro %}

{% macro ratable_cents(amount_col, start_col, end_col, month_col) -%}
{%- set month_first -%}(({{ month_col }}) || '-01')::date{%- endset -%}
(
    case
        when ({{ end_col }})::date < ({{ start_col }})::date
            then error('service period ends before it starts')
        else
            {{ c2c_cumulative_cents(amount_col, start_col, end_col, "last_day(" ~ month_first ~ ")") }}
            - {{ c2c_cumulative_cents(amount_col, start_col, end_col, month_first ~ " - 1") }}
    end
)::bigint
{%- endmacro %}
