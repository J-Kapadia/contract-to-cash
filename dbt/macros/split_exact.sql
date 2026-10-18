{#-
  The single definition of the largest-remainder split.

  split_exact(total_col, weight_bps_col, key_col, partition_cols) is a window expression: within
  each partition the total is split in proportion to the weights so that the parts sum to the
  total exactly. Each part starts at total * weight // sum(weights) (floor); the cents left over go
  one each to the rows with the largest remainders, ties by ascending key. Weights need not sum to
  10000. The result is BIGINT cents.

  partition_cols is a column name, a comma-separated list of columns or a list of them; empty
  means one partition. total_col must be the same on every row of a partition and key_col unique
  within it. A partition whose weights sum to zero gets NULL.

  A window function cannot take another window function as an argument, so the expression
  gathers the partition's (key, weight) pairs once with list() and works on that list.
-#}

{% macro split_exact(total_col, weight_bps_col, key_col, partition_cols='') -%}
{%- if partition_cols is string -%}
    {%- set partition_by = partition_cols -%}
{%- else -%}
    {%- set partition_by = partition_cols | join(', ') -%}
{%- endif -%}
{%- set total = "(" ~ total_col ~ ")::hugeint" -%}
{#- Each row's share before the remainder, with the partition weight sum _ws -#}
{%- set base_of = "(" ~ c2c_floor_div(total ~ " * _e.weight", "_ws") ~ ")" -%}
{%- set own_remainder = "(((" ~ total ~ " * (" ~ weight_bps_col ~ ")::hugeint) % _ws) + _ws) % _ws" -%}
list_extract(
    list_transform(
        [list({'key': {{ key_col }}, 'weight': ({{ weight_bps_col }})::hugeint}) over (
            {%- if partition_by | trim %} partition by {{ partition_by }}{% endif %})],
        _pairs -> list_extract(list_transform(
            [list_sum(list_transform(_pairs, _e -> _e.weight))],
            _ws -> list_extract(list_transform(
                [{{ total }} - list_sum(list_transform(_pairs, _e -> {{ base_of }}))],
                _left -> (
                    {{ c2c_floor_div(total ~ " * (" ~ weight_bps_col ~ ")::hugeint", "_ws") }}
                    + case
                        when list_count(list_filter(_pairs, _f ->
                                (((({{ total }} * _f.weight) % _ws) + _ws) % _ws) > {{ own_remainder }}
                                or ((((({{ total }} * _f.weight) % _ws) + _ws) % _ws) = {{ own_remainder }}
                                    and _f.key < {{ key_col }})
                            )) < _left then 1
                        else 0
                    end
                )::bigint
            ), 1)
        ), 1)
    ),
    1
)
{%- endmacro %}
