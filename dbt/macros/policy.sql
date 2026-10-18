{#-
  policy(key) is one value of the policy configuration, read from `raw.ref_policy` (loaded from
  `config/policy.yaml` before dbt runs), so a weight or threshold has a single definition and SQL
  never repeats a number. The key is the dotted path in the file, for example
  `match_weights.email_exact`; list items carry their position (`aging_buckets_days.0`).

  The result is a scalar subquery of type BIGINT: NULL for a key the configuration does not hold,
  so a typo shows up as a missing number and not as a silent default.
-#}

{% macro policy(key) -%}
(select value from raw.ref_policy where key = '{{ key }}')
{%- endmacro %}
