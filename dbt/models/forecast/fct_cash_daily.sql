{#-
  Forecast cash per case, day and line in the 91 days after the as-of date, as positive cents with
  its direction: the cash in of `fc_cash_in`, the cash out of `fc_cash_out` and the commissions of
  `fc_commissions`, the last as the line `commissions`.
-#}
select as_of, "case", date, line, 'in' as direction, cents
from {{ ref('fc_cash_in') }}

union all

select as_of, "case", date, line, 'out' as direction, cents
from {{ ref('fc_cash_out') }}

union all

select as_of, "case", date, 'commissions' as line, 'out' as direction, cents
from {{ ref('fc_commissions') }}
