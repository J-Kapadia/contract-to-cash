"""The family-driven provenance writer: every mart number has one record, digests follow their inputs."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from c2c import db, prov
from forecast_helpers import mini_closed, mini_closed_copy  # noqa: F401  (fixtures)

MONTH = "2026-03"

MART_FAMILIES = {
    "cust": "SELECT 'cust/' || :m || '/' || customer_id || '/members', member_count::BIGINT"
            " FROM mart.dim_customer WHERE customer_id IS NOT NULL",
    "rev": "SELECT 'rev/' || month || '/' || customer_id || '/' || line_type, recognised_cents"
           " FROM mart.fct_revenue WHERE month = :m",
    "defrev": "SELECT 'defrev/' || month || '/' || customer_id || '/' || metric, cents FROM mart.fct_defrev_rollforward,"
              " LATERAL (VALUES ('opening', opening_cents), ('billings', additions_cents),"
              " ('recognised', reductions_cents), ('closing', closing_cents)) AS v (metric, cents) WHERE month = :m",
    "ar": "SELECT 'ar/' || month || '/' || customer_id || '/' || metric, cents FROM mart.fct_ar_rollforward,"
          " LATERAL (VALUES ('opening', opening_cents), ('billed', billed_cents), ('collected', collected_cents),"
          " ('credits', credits_cents), ('closing', closing_cents)) AS v (metric, cents) WHERE month = :m",
    "cash": "SELECT 'cash/' || month || '/' || metric, cents FROM mart.fct_cash_rollforward,"
            " LATERAL (VALUES ('opening', opening_cents), ('receipts', receipts_cents),"
            " ('disbursements', disbursements_cents), ('closing', closing_cents)) AS v (metric, cents) WHERE month = :m",
    "tie": "SELECT 'tie/' || month || '/' || gl_account, diff_cents FROM mart.fct_gl_tieout WHERE month = :m",
    "bridge": "SELECT 'bridge/' || month || '/' || line, cents FROM mart.fct_bridge WHERE month = :m",
}


def mart_values(con, family: str) -> dict[str, int]:
    return dict(con.execute(MART_FAMILIES[family].replace(":m", f"'{MONTH}'")).fetchall())


def recorded(con, family: str) -> list[tuple]:
    return con.execute("SELECT number_id, value_cents FROM prov.records WHERE split_part(number_id, '/', 1) = ?"
                       " AND split_part(number_id, '/', 2) = ? ORDER BY number_id", [family, MONTH]).fetchall()


@pytest.mark.parametrize("family", sorted(MART_FAMILIES))
def test_every_mart_row_has_exactly_one_record_with_its_value(mini_closed, family):
    with db.connect(mini_closed, read_only=True) as con:
        expected = mart_values(con, family)
        records = recorded(con, family)
    assert expected, f"mini has no {family} rows to check"
    assert records == sorted(expected.items())


def test_reference_and_configuration_rows_have_records(mini_closed):
    with db.connect(mini_closed, read_only=True) as con:
        policy = dict(con.execute("SELECT 'cfg/' || key, value FROM raw.ref_policy").fetchall())
        records = dict(con.execute("SELECT number_id, value_cents FROM prov.records"
                                   " WHERE starts_with(number_id, 'cfg/')").fetchall())
        (accounts,) = con.execute("SELECT count(*) FROM raw.ref_gl_accounts").fetchone()
        (recorded_accounts,) = con.execute("SELECT count(*) FROM prov.records"
                                           " WHERE starts_with(number_id, 'ref/gl_accounts/')").fetchone()
        (pair,) = con.execute("SELECT value_text FROM prov.records WHERE starts_with(number_id, 'ref/customer_matches/')"
                              " ORDER BY number_id LIMIT 1").fetchone()
    assert policy and records == policy
    assert accounts and recorded_accounts == accounts and pair in ("same", "different")


def test_a_record_with_a_source_query_names_the_rows_the_digest_covers(mini_closed):
    with db.connect(mini_closed, read_only=True) as con:
        rows = con.execute("SELECT number_id, input_digest, input_row_count, source_query FROM prov.records"
                           " WHERE source_query IS NOT NULL AND (month = ? OR month IS NULL)", [MONTH]).fetchall()
        assert rows
        for number_id, input_digest, count, query in rows:
            digest, found = prov.source_digest(con, query)
            assert (digest, found) == (input_digest, count), number_id


def test_a_bridge_line_is_a_composite_over_the_months_numbers(mini_closed):
    with db.connect(mini_closed, read_only=True) as con:
        (digest, count, query) = con.execute(
            "SELECT input_digest, input_row_count, source_query FROM prov.records WHERE number_id = ?",
            [f"bridge/{MONTH}/revenue"]).fetchone()
        revenue = con.execute("SELECT number_id, input_digest FROM prov.records WHERE starts_with(number_id, ?)"
                              " ORDER BY number_id", [f"rev/{MONTH}/"]).fetchall()
    assert query is None and count == len(revenue) > 0
    assert digest == prov.pair_digest(revenue)


def test_two_closes_of_the_same_books_give_equal_result_hashes(mini_closed_copy):
    with db.connect(mini_closed_copy) as con:
        (closed_hash,) = con.execute("SELECT result_hash FROM state.close_state WHERE month = ?", [MONTH]).fetchone()
        prov.write_month(con, MONTH, ["CTL-A"], "second-run")
        assert prov.result_hash(con, MONTH) == closed_hash
    assert closed_hash


def test_changing_the_commit_does_not_change_the_hash(mini_closed_copy, monkeypatch):
    with db.connect(mini_closed_copy) as con:
        before = prov.result_hash(con, MONTH)
        monkeypatch.setenv("C2C_GIT_SHA", "0" * 40)
        monkeypatch.setenv("C2C_PRODUCER_KIND", "agent")
        prov.write_month(con, MONTH, [], "other-run")
        shas = con.execute("SELECT DISTINCT git_sha, producer_kind FROM prov.records WHERE month = ?", [MONTH]).fetchall()
        after = prov.result_hash(con, MONTH)
    assert shas == [("0" * 40, "agent")]
    assert before == after


def test_a_changed_value_changes_the_hash(mini_closed_copy):
    with db.connect(mini_closed_copy) as con:
        before = prov.result_hash(con, MONTH)
        con.execute("UPDATE prov.records SET value_cents = value_cents + 1 WHERE number_id = ?",
                    [f"cash/{MONTH}/closing"])
        assert prov.result_hash(con, MONTH) != before


def test_a_month_without_records_has_no_hash(mini_closed):
    with db.connect(mini_closed, read_only=True) as con:
        assert prov.result_hash(con, "1999-01") is None


# ------------------------------------------------------------------ the contract, on a family dir of our own

LEAF = """
select 'demo/' || $month || '/leaf' as number_id, $month as month, {value} as value_cents, null::varchar as value_text,
       'cents' as unit, 'leaf' as kind,
       'select * from (values (''{row}'')) as t (_row_hash)' as source_query, null::varchar[] as children
"""
COMPOSITE = """
select 'sum/' || $month as number_id, $month as month, 0::bigint as value_cents, null::varchar as value_text,
       'cents' as unit, 'composite' as kind, null::varchar as source_query,
       ['demo/' || $month || '/leaf'] as children
"""


def families(tmp_path: Path, **files: str) -> Path:
    directory = tmp_path / "families"
    shutil.rmtree(directory, ignore_errors=True)
    directory.mkdir()
    for name, sql in files.items():
        (directory / f"{name}.sql").write_text(sql, encoding="utf-8")
    return directory


def test_a_composite_digest_changes_when_a_child_digest_does(tmp_path):
    with db.connect(tmp_path / "t.duckdb") as con:
        digests = []
        for row in ("h1", "h2"):
            directory = families(tmp_path, demo=LEAF.format(value=1, row=row), sum=COMPOSITE)
            assert prov.write_month(con, MONTH, [], "run", directory) == 2
            digests.append(dict(con.execute("SELECT number_id, input_digest FROM prov.records").fetchall()))
    first, second = digests
    assert first["demo/2026-03/leaf"] != second["demo/2026-03/leaf"]
    assert first["sum/2026-03"] != second["sum/2026-03"]
    assert first["sum/2026-03"] == prov.pair_digest([("demo/2026-03/leaf", first["demo/2026-03/leaf"])])


def test_a_composite_may_name_a_child_of_a_later_family(tmp_path):
    with db.connect(tmp_path / "t.duckdb") as con:
        directory = families(tmp_path, a_sum=COMPOSITE.replace("'sum/'", "'asum/'"), z_leaf=LEAF.format(value=1, row="h"))
        assert prov.write_month(con, MONTH, [], "run", directory) == 2


def test_an_unknown_kind_fails_before_anything_is_written(tmp_path):
    bad = LEAF.format(value=1, row="h").replace("'leaf' as kind", "'branch' as kind")
    with db.connect(tmp_path / "t.duckdb") as con:
        directory = families(tmp_path, demo=bad)
        with pytest.raises(prov.FamilyError, match="branch"):
            prov.write_month(con, MONTH, [], "run", directory)
        assert con.execute("SELECT count(*) FROM prov.records").fetchone() == (0,)


def test_a_composite_over_a_missing_number_fails(tmp_path):
    with db.connect(tmp_path / "t.duckdb") as con:
        with pytest.raises(prov.FamilyError, match="demo/2026-03/leaf"):
            prov.write_month(con, MONTH, [], "run", families(tmp_path, sum=COMPOSITE))
        assert con.execute("SELECT count(*) FROM prov.records").fetchone() == (0,)


def test_a_number_named_by_two_rows_fails(tmp_path):
    with db.connect(tmp_path / "t.duckdb") as con:
        directory = families(tmp_path, demo=LEAF.format(value=1, row="h"), demo2=LEAF.format(value=2, row="h"))
        with pytest.raises(prov.FamilyError, match="already produced"):
            prov.write_month(con, MONTH, [], "run", directory)


def test_a_number_that_leaves_its_family_loses_its_record(tmp_path):
    leaf = LEAF.format(value=1, row="h")
    with db.connect(tmp_path / "t.duckdb") as con:
        assert prov.write_month(con, MONTH, [], "run", families(tmp_path, demo=leaf)) == 1
        prov.write_month(con, MONTH, [], "run", families(tmp_path, demo=leaf.replace("/leaf'", "/renamed'")))
        assert con.execute("SELECT number_id FROM prov.records").fetchall() == [("demo/2026-03/renamed",)]
