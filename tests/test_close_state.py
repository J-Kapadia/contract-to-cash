"""Month state on a temporary warehouse: the tables, the moves between statuses, the input digest
of a month's books rows, and the check that a warehouse keeps to one set of books."""

from __future__ import annotations

import shutil

import pytest

from c2c import db, load, state
from c2c.errors import InternalError, UsageError

from conftest import MINI_BOOKS, MINI_WINDOW, c2c, signed_config

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


# ------------------------------------------------------------------ close --month on a copy of the books

MONTHS = ["2024-10", "2024-11", "2024-12"]


class Scratch:
    """A temporary warehouse and a signed copy of the configuration, for one books directory."""

    def __init__(self, tmp_path, books):
        self.books = books
        self.warehouse = tmp_path / "w.duckdb"
        self.config, self.signoff = signed_config(tmp_path)

    def close(self, month: str):
        return c2c("close", "--month", month, "--books", str(self.books), "--warehouse", str(self.warehouse),
                   "--config", str(self.config), "--json", env={"C2C_SIGNOFF_DIR": str(self.signoff)})

    def query(self, sql: str):
        with db.connect(self.warehouse, read_only=True) as con:
            return con.execute(sql).fetchall()

    def statuses(self) -> dict[str, str]:
        return dict(self.query("SELECT month, status FROM state.close_state ORDER BY month"))


@pytest.fixture(scope="module")
def closed_through_december(tmp_path_factory):
    scratch = Scratch(tmp_path_factory.mktemp("closed-state"), MINI_BOOKS)
    first = scratch.close("2024-12")
    assert first.code == 0, first.stderr
    return scratch, first.json


def test_closing_december_closes_october_to_december_in_order(closed_through_december):
    scratch, done = closed_through_december
    assert done["close_status"] == "closed"
    assert scratch.statuses() == {m: "closed" for m in MONTHS}
    assert [m for (m,) in scratch.query("SELECT DISTINCT month FROM ctl.control_results ORDER BY month")] == MONTHS
    stored = scratch.query("SELECT month, input_digest, result_hash, restated_from FROM state.close_state ORDER BY month")
    assert all(digest and result and restated is None for _, digest, result, restated in stored)
    assert done["result_hash"] == stored[-1][2]
    assert len({digest for _, digest, _, _ in stored}) == len(MONTHS)


def test_a_second_close_is_unchanged_and_records_nothing(closed_through_december):
    scratch, first = closed_through_december
    before = scratch.query("SELECT * FROM state.close_state ORDER BY month")
    results = scratch.query("SELECT count(*) FROM ctl.control_results")

    again = scratch.close("2024-12")

    assert again.code == 0, again.stderr
    assert again.json["close_status"] == "unchanged"
    assert again.json["result_hash"] == first["result_hash"]
    assert again.json["controls"] == first["controls"]
    assert scratch.query("SELECT * FROM state.close_state ORDER BY month") == before
    assert scratch.query("SELECT count(*) FROM ctl.control_results") == results


def test_an_earlier_month_that_is_closed_is_unchanged_when_asked_for_again(closed_through_december):
    scratch, _ = closed_through_december
    again = scratch.close("2024-11")
    assert again.code == 0, again.stderr
    assert again.json["close_status"] == "unchanged"
    assert scratch.statuses() == {m: "closed" for m in MONTHS}


def test_the_memo_of_a_closed_month_is_written(closed_through_december, tmp_path):
    scratch, _ = closed_through_december
    site = tmp_path / "site"
    memo = c2c("memo", "--month", "2024-12", "--out", str(site), "--warehouse", str(scratch.warehouse),
               "--books", str(scratch.books), "--config", str(scratch.config), "--json",
               env={"C2C_SIGNOFF_DIR": str(scratch.signoff)})
    assert memo.code == 0, memo.stderr
    assert (site / "2024-12" / "index.html").is_file()


def test_the_memo_of_an_open_month_exits_4_telling_the_user_to_close_it(tmp_path):
    scratch = Scratch(tmp_path, MINI_BOOKS)
    assert scratch.close("2024-10").code == 0
    site = tmp_path / "site"
    memo = c2c("memo", "--month", "2024-11", "--out", str(site), "--warehouse", str(scratch.warehouse),
               "--books", str(scratch.books), "--config", str(scratch.config), "--json",
               env={"C2C_SIGNOFF_DIR": str(scratch.signoff)})
    assert memo.code == 4, memo.stderr
    assert "not closed" in memo.json["message"]
    assert "close --month 2024-11" in memo.json["message"]
    assert not site.exists()


@pytest.mark.parametrize("month", ["2024-09", "2026-04"])
def test_a_month_outside_the_window_exits_2_naming_the_range(tmp_path, month):
    scratch = Scratch(tmp_path, MINI_BOOKS)
    done = scratch.close(month)
    assert done.code == 2, done.stderr
    assert f"{FIRST}..{LAST}" in done.json["message"]
    assert not scratch.warehouse.exists()


def test_a_warehouse_holding_other_books_exits_2(closed_through_december, tmp_path):
    scratch, _ = closed_through_december
    other = tmp_path / "other"
    assert c2c("gen", "--fixture", "mini", "--seed", "2", "--out", str(other)).code == 0
    copy = Scratch(tmp_path, other / "books")
    shutil.copy(scratch.warehouse, copy.warehouse)

    done = copy.close("2024-12")

    assert done.code == 2, done.stderr
    assert "holds books" in done.json["message"]


def test_a_control_failure_in_november_leaves_it_and_december_open(tmp_path):
    books = tmp_path / "books"
    shutil.copytree(MINI_BOOKS, books)
    path = books / "gl.csv"
    lines = path.read_text(encoding="utf-8").splitlines()
    header = lines[0].split(",")
    period, account, amount = header.index("gl_period"), header.index("gl_account"), header.index("amount_cents")
    index = next(i for i, line in enumerate(lines[1:], 1)
                 if line.split(",")[period] == "2024-11" and line.split(",")[account] == "1200")
    fields = lines[index].split(",")
    fields[amount] = str(int(fields[amount]) + 100)
    lines[index] = ",".join(fields)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    scratch = Scratch(tmp_path, books)

    done = scratch.close("2024-12")

    assert done.code == 3, done.stderr
    assert [c["id"] for c in done.json["failed_controls"]] == ["CTL-TIE-AR"]
    assert "2024-11" in done.json["message"]
    assert scratch.statuses() == {"2024-10": "closed", "2024-11": "open", "2024-12": "open"}
