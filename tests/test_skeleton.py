"""The skeleton end to end: close a month, write the memo, resolve every number it carries."""

from __future__ import annotations

import re
import shutil
import time

import pytest

from c2c import db, lock, memo
from c2c.errors import LockHeld, UsageError

from conftest import CLOSED_MONTH, Closed, c2c

NUM_ID = re.compile(r'data-num-id="([^"]+)"')


def copy_of(closed: Closed, warehouse) -> None:
    """Copy the shared closed warehouse once no close holds it."""
    for _ in range(600):
        try:
            with lock.held(closed.warehouse, "shared"):
                shutil.copy(closed.warehouse, warehouse)
            return
        except LockHeld:
            time.sleep(0.1)
    pytest.fail("the closed warehouse stayed locked")


@pytest.fixture(scope="module")
def page(closed, tmp_path_factory):
    root = tmp_path_factory.mktemp("skeleton")
    warehouse = root / "w.duckdb"
    copy_of(closed, warehouse)
    out = root / "site"
    done = c2c("memo", "--month", CLOSED_MONTH, "--out", str(out), "--warehouse", str(warehouse))
    assert done.code == 0, done.stderr
    return Closed(books=closed.books, warehouse=warehouse), (out / CLOSED_MONTH / "index.html").read_text()


def test_the_memo_is_marked_synthetic_and_carries_numbers(page):
    _, html = page
    assert "(synthetic)" in html
    assert NUM_ID.search(html)


def test_every_number_in_the_page_resolves_through_explain(page):
    where, html = page
    ids = sorted(set(NUM_ID.findall(html)))
    assert ids
    for number_id in ids:
        done = c2c("explain", number_id, "--warehouse", str(where.warehouse))
        assert done.code == 0, f"{number_id}: {done.stderr}"


def test_the_page_value_is_the_recorded_value(page):
    where, html = page
    with db.connect(where.warehouse, read_only=True) as con:
        recorded = {k: v[0] for k, v in memo.records(con).items()}
    for number_id, value in re.findall(r'data-num-id="([^"]+)" data-value="([^"]*)"', html):
        assert value == str(recorded[number_id])


def test_a_month_that_is_not_closed_exits_4(closed, tmp_path):
    warehouse = tmp_path / "w.duckdb"
    copy_of(closed, warehouse)
    missing = c2c("memo", "--month", "2027-01", "--out", str(tmp_path / "site"), "--warehouse", str(warehouse))
    assert missing.code == 4
    assert not (tmp_path / "site" / "2027-01").exists()


def test_the_num_filter_refuses_an_unknown_id():
    with pytest.raises(UsageError, match="no record"):
        memo.num_filter({})("ar/none")


def test_the_filter_escapes_the_id():
    known = {'a"b': (5, None, "cents")}
    html = memo.num_filter(known)('a"b')
    assert 'data-num-id="a&#34;b"' in html and "$0.05" in html
