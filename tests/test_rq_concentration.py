"""Customer concentration over the closed `mini` books: the rows against the recognised revenue per
customer, the ranks and shares, the shares of the largest one, five and ten customers, and the records
that carry each of them."""

from __future__ import annotations

import shutil

from c2c import db

from conftest import MINI_BOOKS, c2c
from forecast_helpers import mini_closed  # noqa: F401  (fixture)

MONTH = "2026-03"
EMPTY_MONTH = "2024-10"
COLUMNS = "customer_id, revenue_cents, total_cents, rank, share_bps, top1_bps, top5_bps, top10_bps"


def query(warehouse, sql, params=()):
    with db.connect(warehouse, read_only=True) as con:
        return con.execute(sql, params).fetchall()


def rows(warehouse, month=MONTH):
    return {r[0]: r[1:] for r in query(
        warehouse, f"SELECT {COLUMNS} FROM mart.fct_concentration WHERE month = ? ORDER BY rank", [month])}


def test_the_customer_rows_equal_the_recognised_revenue_per_customer(mini_closed):
    revenue = dict(query(mini_closed, "SELECT customer_id, sum(recognised_cents)::bigint FROM mart.fct_revenue"
                                      " WHERE month = ? AND customer_id IS NOT NULL GROUP BY customer_id"
                                      " HAVING sum(recognised_cents) <> 0", [MONTH]))
    got = rows(mini_closed)
    assert got and {c: r[0] for c, r in got.items()} == revenue
    assert {r[1] for r in got.values()} == {sum(revenue.values())}


def test_ranks_run_from_one_without_gaps_and_ties_go_by_customer(mini_closed):
    ordered = query(mini_closed, "SELECT customer_id, revenue_cents, rank FROM mart.fct_concentration"
                                 " WHERE month = ? ORDER BY rank", [MONTH])
    assert [r[2] for r in ordered] == list(range(1, len(ordered) + 1))
    keys = [(-revenue, customer) for customer, revenue, _ in ordered]
    assert keys == sorted(keys)


def test_shares_are_floored_and_their_tops_are_ordered(mini_closed):
    got = rows(mini_closed)
    n = len(got)
    assert n <= 10, "mini has at most ten master customers"
    shares = [r[3] for r in got.values()]
    assert 10000 - n <= sum(shares) <= 10000
    for revenue, total, _, share, *_ in got.values():
        assert share == revenue * 10000 // total
    assert len({r[4:] for r in got.values()}) == 1, "the tops repeat on every row"
    top1, top5, top10 = next(iter(got.values()))[4:]
    assert top1 == max(shares) and top1 <= top5 <= top10 and top10 == 10000
    assert top5 == got_top(got, 5)


def got_top(got, n):
    """The floored share of the `n` largest customers, from their revenue."""
    values = list(got.values())
    return sum(v[0] for v in values[:n]) * 10000 // values[0][1]


def test_a_month_without_recognised_revenue_has_no_rows_and_no_record(mini_closed):
    assert query(mini_closed, "SELECT count(*) FROM mart.fct_concentration WHERE month = ?", [EMPTY_MONTH]) == [(0,)]
    assert query(mini_closed, "SELECT number_id FROM prov.records WHERE number_id LIKE ?",
                 [f"concentration/{EMPTY_MONTH}/%"]) == []


def test_every_row_has_its_customer_and_share_records_and_the_tops_exist(mini_closed):
    got = rows(mini_closed)
    records = {n: (v, u) for n, v, u in query(
        mini_closed, "SELECT number_id, value_cents, unit FROM prov.records WHERE number_id LIKE ?",
        [f"concentration/{MONTH}/%"])}
    expected = {}
    for customer, (revenue, _, _, share, top1, top5, top10) in got.items():
        expected[f"concentration/{MONTH}/customer/{customer}"] = (revenue, "cents")
        expected[f"concentration/{MONTH}/share/{customer}"] = (share, "bps")
    top1, top5, top10 = next(iter(got.values()))[4:]
    expected |= {f"concentration/{MONTH}/top1": (top1, "bps"), f"concentration/{MONTH}/top5": (top5, "bps"),
                 f"concentration/{MONTH}/top10": (top10, "bps")}
    assert records == expected


def test_explain_resolves_the_largest_customers_share(mini_closed, tmp_path):
    copy = tmp_path / "w.duckdb"
    shutil.copyfile(mini_closed, copy)
    done = c2c("explain", f"concentration/{MONTH}/top1", "--json", "--books", str(MINI_BOOKS), "--warehouse", str(copy))
    assert done.code == 0, done.stderr
    chain = done.json["explanation"]
    assert chain["digest_matches"] is True and chain["record"]["step"] == "fct_concentration"
