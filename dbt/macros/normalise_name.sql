{#-
  The single definition of a normalised customer name.

  normalise_name(col) lowercases the text, puts it in Unicode NFC form, turns every run of
  characters that are not letters or digits into one space, drops the tokens that are legal
  suffixes (`raw.ref_legal_suffixes`) and joins what is left with single spaces. NULL stays NULL;
  a name made only of suffixes becomes the empty string.

  Applying it to its own output changes nothing: the output holds lowercase letters and digits
  separated by single spaces, none of them a suffix.

  The suffix list is read once, outside the token filter, because DuckDB does not accept a
  subquery inside a lambda. The argument is a SQL expression and appears once in the output.
-#}

{% macro normalise_name(col) -%}
list_extract(
    list_transform(
        [(select list(lower(trim(suffix))) from {{ source('raw', 'ref_legal_suffixes') }})],
        _suffixes -> array_to_string(
            list_filter(
                string_split(
                    trim(regexp_replace(nfc_normalize(lower({{ col }})), '[^\p{L}\p{N}]+', ' ', 'g')),
                    ' '),
                _token -> _token != '' and not list_contains(_suffixes, _token)),
            ' ')
    ),
    1
)
{%- endmacro %}
