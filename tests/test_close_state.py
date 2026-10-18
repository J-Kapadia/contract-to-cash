"""Month state on a temporary warehouse: the tables, the moves between statuses, the input digest
of a month's books rows, and the check that a warehouse keeps to one set of books."""

from __future__ import annotations

import shutil

import pytest

from c2c import db, load, state
from c2c.errors import InternalError, UsageError

from conftest import MINI_BOOKS, MINI_WINDOW

FIRST, LAST = MINI_WINDOW


@pytest.fixture
def con(tmp_path):
    connection = db.connect(tmp_path / "w.duckdb")
    db.init_schemas(connection)
    yield connection
    connection.close()


def _load(con, books, last=LAST):
    load.load_months(con, books, FIRST, last, "run")


def test_init_is_idempotent_and_keeps_rows(con):
    state.init(con)
    state.transition(con, FIRST, state.CLOSED, "d", "h")
    state.init(con)
    assert state.get(con, FIRST) == {"status": "closed", "input_digest": "d", "result_hash": "h",
                                     "restated_from": None}
    assert con.execute("SELECT count(*) FROM state.close_state").fetchone() == (1,)


def test_a_month_with_no_row_is_open(con):
    assert state.get(con, FIRST) is None
    assert state.status(con, FIRST) == state.OPEN
    state.init(con)
    assert state.get(con, FIRST) is None


def test_months_runs_across_a_year_end():
    assert list(state.months("2024-11", "2025-02")) == ["2024-11", "2024-12", "2025-01", "2025-02"]
    assert list(state.months("2024-12", "2024-12")) == ["2024-12"]
    assert list(state.months("2025-01", "2024-12")) == []


def test_transition_close_restate_lock_and_reopen(con):
    state.transition(con, FIRST, state.OPEN)
    state.transition(con, FIRST, state.CLOSED, "d1")
    assert state.get(con, FIRST)["result_hash"] is None
    state.transition(con, FIRST, state.CLOSED, "d2", "h2", restated_from="h1")
    assert state.get(con, FIRST) == {"status": "closed", "input_digest": "d2", "result_hash": "h2",
                                     "restated_from": "h1"}
    state.transition(con, FIRST, state.OPEN)
    assert state.get(con, FIRST) == {"status": "open", "input_digest": None, "result_hash": None,
                                     "restated_from": None}
    state.transition(con, FIRST, state.CLOSED, "d3")
    state.transition(con, FIRST, state.LOCKED)
    assert state.status(con, FIRST) == state.LOCKED


@pytest.mark.parametrize("path", [
    [state.LOCKED],
    [state.CLOSED, state.LOCKED, state.CLOSED],
    [state.CLOSED, state.LOCKED, state.OPEN],
])
def test_a_move_the_state_machine_does_not_allow_raises(con, path):
    with pytest.raises(InternalError):
        for to in path:
            state.transition(con, FIRST, to, "d")


def test_an_unknown_status_raises(con):
    with pytest.raises(InternalError):
        state.transition(con, FIRST, "archived")


def test_input_digest_is_stable_across_calls(con):
    _load(con, MINI_BOOKS)
    digest = state.input_digest(con, FIRST)
    assert len(digest) == 64
    assert state.input_digest(con, FIRST) == digest
    _load(con, MINI_BOOKS)  # a reload of the same books
    assert state.input_digest(con, FIRST) == digest


def test_input_digest_changes_with_a_row_of_its_month_only(con, tmp_path):
    _load(con, MINI_BOOKS)
    before = {m: state.input_digest(con, m) for m in state.months(FIRST, LAST)}
    assert len(set(before.values())) == len(before)

    books = tmp_path / "books"
    shutil.copytree(MINI_BOOKS, books)
    gl = books / "gl.csv"
    lines = gl.read_text(encoding="utf-8").splitlines()
    header = lines[0].split(",")
    amount = next(i for i, name in enumerate(header) if name.endswith("cents") or name == "amount")
    index = next(i for i, line in enumerate(lines) if i and line.split(",")[3] == "2024-11")
    fields = lines[index].split(",")
    fields[amount] = str(int(fields[amount]) + 100)
    lines[index] = ",".join(fields)
    gl.write_text("\n".join(lines) + "\n", encoding="utf-8")
    _load(con, books)

    after = {m: state.input_digest(con, m) for m in before}
    assert after["2024-11"] != before["2024-11"]
    assert {m: d for m, d in after.items() if m != "2024-11"} == {m: d for m, d in before.items() if m != "2024-11"}


def test_input_digest_ignores_months_loaded_later(con):
    _load(con, MINI_BOOKS, last="2024-11")
    early = state.input_digest(con, FIRST)
    _load(con, MINI_BOOKS)
    assert state.input_digest(con, FIRST) == early


def test_a_month_with_no_rows_has_a_digest_of_nothing(con):
    _load(con, MINI_BOOKS)
    assert state.input_digest(con, "1999-01") == state.input_digest(con, "1999-02")


def _books_with_manifest(root, text):
    books = root / "books"
    books.mkdir(parents=True)
    (root / "books.manifest.sha256").write_text(text, encoding="utf-8")
    return books


def test_check_books_records_then_accepts_the_same_books(con, tmp_path):
    books = _books_with_manifest(tmp_path / "a", "one\n")
    state.check_books(con, books)
    state.check_books(con, books)
    assert con.execute("SELECT books_id FROM state.books").fetchall() == [(state.books_id(books),)]


def test_a_warehouse_holding_different_books_is_a_usage_error(con, tmp_path):
    state.check_books(con, _books_with_manifest(tmp_path / "a", "one\n"))
    with pytest.raises(UsageError) as raised:
        state.check_books(con, _books_with_manifest(tmp_path / "b", "two\n"))
    assert raised.value.exit == 2
    assert "holds books" in raised.value.message


def test_books_without_a_manifest_have_no_identity(con, tmp_path):
    bare = tmp_path / "bare" / "books"
    bare.mkdir(parents=True)
    assert state.books_id(bare) is None
    state.check_books(con, bare)
    assert con.execute("SELECT count(*) FROM state.books").fetchone() == (0,)
    state.check_books(con, _books_with_manifest(tmp_path / "a", "one\n"))
    state.check_books(con, bare)  # still not refused once the warehouse holds other books
