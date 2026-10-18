{#- A model's `+schema` is the schema itself (`mart`, `int`), not a suffix on the target's. -#}
{% macro generate_schema_name(custom_schema_name, node) -%}
    {{ (custom_schema_name | trim) if custom_schema_name is not none else target.schema }}
{%- endmacro %}
