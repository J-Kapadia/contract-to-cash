"""`close --recompute`: a closed month is rebuilt and its previous result hash is kept as the restated one;
a locked month exits 4, and the flag cannot be combined with `--check`."""

from __future__ import annotations

import datetime
import shutil

import pytest

from c2c import db, state

from conftest import MINI_BOOKS, c2c, signed_config

MONTH = "2026-03"
EARLIER = "2026-02"


class Scratch:
    """A signed configuration, a warehouse and a copy of the `mini` books, to close, edit and restate."""

    def __init__(self, tmp_path, books, warehouse=None):
        self.books = books
        self.warehouse = warehouse or tmp_path / "w.duckdb"
        self.config, self.signoff = signed_config(tmp_path)

    def run(self, *args: str):
        return c2c(*args, "--books", str(self.books), "--warehouse", str(self.warehouse),
                   "--config", str(self.config), "--json", env={"C2C_SIGNOFF_DIR": str(self.signoff)})

    def close(self, month: str, *flags: str):
        return self.run("close", "--month", month, *flags)

    def months(self) -> dict[str, dict]:
        return {m["month"]: m for m in self.run("status").json["months"]}

    def edit_due_date(self, month: str) -> None:
        """Push the due date of the first invoice of `month` out by a month: no ledger amount moves, so every
        control still passes, but the receivables figures built from the invoice do."""
        path = self.books / "invoices.csv"
        lines = path.read_text(encoding="utf-8").splitlines()
        header = lines[0].split(",")
        index = next(i for i, line in enumerate(lines[1:], 1) if line.split(",")[header.index("gl_period")] == month)
        fields = lines[index].split(",")
        due = header.index("due_date")
        fields[due] = (datetime.date.fromisoformat(fields[due]) + datetime.timedelta(days=30)).isoformat()
        lines[index] = ",".join(fields)
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    def lock(self, month: str) -> None:
        with db.connect(self.warehouse) as con:
            state.transition(con, month, state.LOCKED)


@pytest.fixture(scope="module")
def template(tmp_path_factory):
    """`mini` closed through its last month."""
    root = tmp_path_factory.mktemp("recompute")
    books = root / "books"
    shutil.copytree(MINI_BOOKS, books)
    scratch = Scratch(root, books)
    done = scratch.close(MONTH)
    assert done.code == 0, done.stderr
    return scratch


@pytest.fixture
def scratch(template, tmp_path):
    """A private copy of the closed warehouse and books."""
    books = tmp_path / "books"
    shutil.copytree(template.books, books)
    warehouse = tmp_path / "w.duckdb"
    shutil.copy(template.warehouse, warehouse)
    return Scratch(tmp_path, books, warehouse)


def test_recompute_after_a_changed_row_stores_the_new_hash_and_the_previous_one(scratch):
    before = scratch.months()[MONTH]
    assert before["status"] == "closed" and before["restated_from"] is None
    scratch.edit_due_date(MONTH)
    done = scratch.close(MONTH, "--recompute")
    assert done.code == 0, done.stderr
    after = scratch.months()[MONTH]
    assert after["status"] == "closed"
    assert after["restated_from"] == before["result_hash"]
    assert after["result_hash"] == done.json["result_hash"]


def test_status_shows_the_restatement_and_leaves_earlier_months_alone(scratch):
    scratch.edit_due_date(MONTH)
    assert scratch.close(MONTH, "--recompute").code == 0
    months = scratch.months()
    assert months[MONTH]["restated_from"] is not None
    assert months[EARLIER]["restated_from"] is None and months[EARLIER]["status"] == "closed"


def test_recompute_with_unchanged_books_restates_nothing(scratch):
    before = scratch.months()[MONTH]
    done = scratch.close(MONTH, "--recompute")
    assert done.code == 0, done.stderr
    after = scratch.months()[MONTH]
    assert after["result_hash"] == before["result_hash"] and after["restated_from"] is None


def test_recompute_on_a_locked_month_exits_4(scratch):
    scratch.lock(MONTH)
    before = scratch.months()[MONTH]
    done = scratch.close(MONTH, "--recompute")
    assert done.code == 4
    assert f"month {MONTH} is locked" in done.json["message"]
    assert scratch.months()[MONTH] == before


def test_recompute_on_a_month_that_is_not_closed_exits_4(workspace):
    shutil.copytree(MINI_BOOKS, workspace.books)
    config, signoff = signed_config(workspace.root)
    done = c2c("close", "--month", MONTH, "--recompute", *workspace.flags(), "--config", str(config), "--json",
               env={"C2C_SIGNOFF_DIR": str(signoff)})
    assert done.code == 4
    assert f"month {MONTH} not closed" in done.json["message"]


def test_recompute_with_check_is_a_usage_error(scratch):
    done = scratch.close(MONTH, "--recompute", "--check")
    assert done.code == 2
    assert "--recompute" in done.json["message"] and "--check" in done.json["message"]
