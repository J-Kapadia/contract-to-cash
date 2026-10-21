"""`c2c forecast --as-of M` on `mini` closed through its last month: what it publishes, that a re-run
replaces it, the scratch schema it leaves nothing of, the month it refuses and the numbers `explain` reaches."""

from __future__ import annotations

import json
import shutil

import pytest

from c2c import db, ids
from conftest import MINI_BOOKS, c2c
from forecast_helpers import mini_closed, mini_closed_copy  # noqa: F401  (fixtures)

AS_OF = "2026-01-31"
CASES = ("base", "downside", "upside")
WEEKS = 13
# Above the lowest ending balance of the first week in every case on `mini`, so every case alerts.
MIN_CASH_CENTS = 260_000_000


def forecast(warehouse, *args: str):
    return c2c("forecast", "--as-of", "2026-01", "--books", str(MINI_BOOKS), "--warehouse", str(warehouse),
               "--json", *args)


def query(warehouse, sql: str, params=()):
    with db.connect(warehouse, read_only=True) as con:
        return con.execute(sql, params).fetchall()


@pytest.fixture(scope="module")
def published(mini_closed, tmp_path_factory):  # noqa: F811
    warehouse = tmp_path_factory.mktemp("forecast-command") / "w.duckdb"
    shutil.copyfile(mini_closed, warehouse)
    with db.connect(warehouse) as con:  # the minimum cash the models read, set so that a case alerts
        con.execute("UPDATE raw.ref_policy SET value = ? WHERE key = 'min_cash_cents'", [MIN_CASH_CENTS])
    done = forecast(warehouse)
    assert done.code == 0, done.stderr
    return warehouse, json.loads(done.stdout)


def test_the_envelope_names_the_as_of_the_cases_and_the_downside_alert(published):
    warehouse, envelope = published
    assert envelope["command"] == "forecast"
    assert envelope["exit"] == 0
    assert envelope["as_of"] == AS_OF
    assert envelope["cases"] == list(CASES)
    alert = {a["case"]: a for a in envelope["alerts"]}["downside"]
    (ending,) = {e for (e,) in query(warehouse, "SELECT ending_cents FROM forecast.weekly WHERE "
                                                "\"case\" = 'downside' AND week = 'W01'")}
    assert alert["first_breach_week"] == "W01"
    assert alert["shortfall_cents"] == MIN_CASH_CENTS - ending


def test_each_case_and_line_has_thirteen_weekly_rows(published):
    warehouse, _ = published
    rows = query(warehouse, 'SELECT "case", line, count(DISTINCT week), count(*) FROM forecast.weekly '
                            "WHERE as_of = ? GROUP BY ALL", [AS_OF])
    assert {case for case, *_ in rows} == set(CASES)
    assert all(weeks == WEEKS and count == WEEKS for _, _, weeks, count in rows)


def test_the_alert_row_matches_the_envelope(published):
    warehouse, envelope = published
    rows = query(warehouse, 'SELECT "case", first_breach_week, shortfall_cents FROM forecast.alerts ORDER BY 1')
    assert rows == [(a["case"], a["first_breach_week"], a["shortfall_cents"]) for a in envelope["alerts"]]


def test_one_run_is_recorded_and_no_scratch_schema_remains(published):
    warehouse, envelope = published
    runs = query(warehouse, "SELECT CAST(as_of AS VARCHAR), forecast_hash, cases FROM forecast.runs")
    assert runs == [(AS_OF, envelope["forecast_hash"], list(CASES))]
    assert query(warehouse, "SELECT count(*) FROM information_schema.schemata WHERE schema_name = 'fc_build'") == [(0,)]


def test_status_lists_the_run(published):
    warehouse, _ = published
    done = c2c("status", "--warehouse", str(warehouse), "--json")
    assert done.code == 0, done.stderr
    assert [r["as_of"] for r in json.loads(done.stdout)["forecast_runs"]] == [AS_OF]


def test_every_recorded_forecast_rate_and_config_id_parses(published):
    warehouse, _ = published
    recorded = query(warehouse, "SELECT number_id FROM prov.records WHERE split_part(number_id, '/', 1) IN "
                                "('fc', 'rate', 'cfg')")
    families = {n.split("/")[0] for (n,) in recorded}
    assert families == {"fc", "rate", "cfg"}
    for (number_id,) in recorded:
        assert ids.is_valid(number_id), number_id


def test_explain_resolves_a_forecast_a_rate_and_a_config_number(published):
    warehouse, _ = published
    wanted = [
        f"fc/{AS_OF}/base/cash/W01/payroll",
        f"fc/{AS_OF}/downside/alert/first_breach_week",
        f"fc/{AS_OF}/base/cash/W13/ending",
        f"rate/{AS_OF}/renewal/first",
        "cfg/cases.base.cloud_up_bps",
    ]
    for number_id in wanted:
        done = c2c("explain", number_id, "--books", str(MINI_BOOKS), "--warehouse", str(warehouse), "--json")
        assert done.code == 0, (number_id, done.stderr)
        assert json.loads(done.stdout)["explanation"]["digest_matches"] is True, number_id


def test_running_again_replaces_the_rows_without_duplicates(mini_closed_copy):  # noqa: F811
    first = forecast(mini_closed_copy)
    assert first.code == 0, first.stderr
    counts = {t: query(mini_closed_copy, f"SELECT count(*) FROM forecast.{t}")[0][0]
              for t in ("inputs", "rates", "alerts", "daily", "weekly", "revenue", "runs")}
    records = query(mini_closed_copy, "SELECT count(*) FROM prov.records")[0][0]
    second = forecast(mini_closed_copy)
    assert second.code == 0, second.stderr
    assert counts == {t: query(mini_closed_copy, f"SELECT count(*) FROM forecast.{t}")[0][0] for t in counts}
    assert query(mini_closed_copy, "SELECT count(*) FROM prov.records")[0][0] == records
    assert counts["runs"] == 1
    assert json.loads(first.stdout)["forecast_hash"] == json.loads(second.stdout)["forecast_hash"]


def test_case_publishes_only_the_cases_named(mini_closed_copy):  # noqa: F811
    done = forecast(mini_closed_copy, "--case", "base", "--case", "downside")
    assert done.code == 0, done.stderr
    envelope = json.loads(done.stdout)
    assert envelope["cases"] == ["base", "downside"]
    for table in ("weekly", "daily", "revenue", "inputs"):
        assert {c for (c,) in query(mini_closed_copy, f'SELECT DISTINCT "case" FROM forecast.{table}')} == \
            {"base", "downside"}
    assert query(mini_closed_copy, "SELECT cases FROM forecast.runs") == [(["base", "downside"],)]


def test_a_month_that_is_not_closed_exits_4(mini_closed_copy):  # noqa: F811
    done = c2c("forecast", "--as-of", "2026-04", "--books", str(MINI_BOOKS), "--warehouse", str(mini_closed_copy),
               "--json")
    assert done.code == 4
    assert "not closed" in json.loads(done.stdout)["message"]
    assert query(mini_closed_copy, "SELECT count(*) FROM information_schema.schemata "
                                   "WHERE schema_name IN ('fc_build')") == [(0,)]


def test_a_warehouse_that_does_not_exist_exits_4(tmp_path):
    done = forecast(tmp_path / "none.duckdb")
    assert done.code == 4
