"""Locking closed months: `state.lock_through` freezes the months up to one, and a changed row in a
locked month fails the close with CTL-LOCK, before the warehouse is touched; an unchanged re-close
is a no-op and an edit in the open month closes normally."""

from __future__ import annotations

import shutil

import pytest

from c2c import controls, db, state
from c2c.errors import ControlFailure

from conftest import MINI_BOOKS, c2c, signed_config

LOCKED_THROUGH, OPEN_MONTH = "2026-02", "2026-03"
LOCKED = ["2024-10", "2024-11", "2024-12", "2025-01", "2025-02", "2025-03", "2025-04", "2025-05", "2025-06",
          "2025-07", "2025-08", "2025-09", "2025-10", "2025-11", "2025-12", "2026-01", "2026-02"]


class Scratch:
    """A signed configuration, a warehouse and a copy of the `mini` books, to close and to edit."""

    def __init__(self, tmp_path, books, warehouse=None):
        self.books = books
        self.warehouse = warehouse or tmp_path / "w.duckdb"
        self.config, self.signoff = signed_config(tmp_path)

    def close(self, month: str):
        return c2c("close", "--month", month, "--books", str(self.books), "--warehouse", str(self.warehouse),
                   "--config", str(self.config), "--json", env={"C2C_SIGNOFF_DIR": str(self.signoff)})

    def query(self, sql: str):
        with db.connect(self.warehouse, read_only=True) as con:
            return con.execute(sql).fetchall()

    def statuses(self) -> dict[str, str]:
        return dict(self.query("SELECT month, status FROM state.close_state ORDER BY month"))

    def edit_payroll(self, month: str) -> None:
        """Add a head to the first payroll run of `month`; no control depends on the headcount."""
        path = self.books / "payroll_runs.csv"
        lines = path.read_text(encoding="utf-8").splitlines()
        header = lines[0].split(",")
        index = next(i for i, line in enumerate(lines[1:], 1) if line.split(",")[0].startswith(month))
        fields = lines[index].split(",")
        fields[header.index("headcount")] = str(int(fields[header.index("headcount")]) + 1)
        lines[index] = ",".join(fields)
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")


@pytest.fixture(scope="module")
def locked_template(tmp_path_factory):
    """`mini` closed through its last month, then locked through 2026-02."""
    root = tmp_path_factory.mktemp("locked")
    books = root / "books"
    shutil.copytree(MINI_BOOKS, books)
    scratch = Scratch(root, books)
    done = scratch.close(OPEN_MONTH)
    assert done.code == 0, done.stderr
    with db.connect(scratch.warehouse) as con:
        assert state.lock_through(con, LOCKED_THROUGH) == LOCKED
    return scratch, done.json


@pytest.fixture
def scratch(locked_template, tmp_path):
    """A private copy of the locked warehouse and books."""
    template, _ = locked_template
    books = tmp_path / "books"
    shutil.copytree(template.books, books)
    warehouse = tmp_path / "w.duckdb"
    shutil.copy(template.warehouse, warehouse)
    return Scratch(tmp_path, books, warehouse)


def test_lock_through_locks_every_closed_month_up_to_it(locked_template):
    template, _ = locked_template
    assert template.statuses() == {**{m: "locked" for m in LOCKED}, OPEN_MONTH: "closed"}
    rows = template.query("SELECT month, status, input_digest IS NOT NULL, result_hash IS NOT NULL"
                          " FROM state.close_state ORDER BY month")
    assert all(digest and result for _, _, digest, result in rows)


def test_lock_through_is_idempotent_and_leaves_open_months(tmp_path):
    with db.connect(tmp_path / "w.duckdb") as con:
        state.transition(con, "2024-10", state.CLOSED, "d1", "h1")
        state.transition(con, "2024-11", state.OPEN)
        state.transition(con, "2024-12", state.CLOSED, "d3", "h3")
        assert state.lock_through(con, "2024-11") == ["2024-10"]
        assert state.lock_through(con, "2024-11") == []
        assert state.get(con, "2024-10") == {"status": "locked", "input_digest": "d1", "result_hash": "h1",
                                             "restated_from": None}
        assert [state.status(con, m) for m in ("2024-11", "2024-12")] == [state.OPEN, state.CLOSED]
        assert state.locked_months(con) == {"2024-10": "d1"}


def test_an_unchanged_re_close_is_a_no_op(locked_template, scratch):
    _, first = locked_template
    before = scratch.query("SELECT * FROM state.close_state ORDER BY month")
    results = scratch.query("SELECT count(*) FROM ctl.control_results")

    again = scratch.close(OPEN_MONTH)

    assert again.code == 0, again.stderr
    assert again.json["close_status"] == "unchanged"
    assert again.json["result_hash"] == first["result_hash"]
    assert scratch.query("SELECT * FROM state.close_state ORDER BY month") == before
    assert scratch.query("SELECT count(*) FROM ctl.control_results") == results


def test_an_edit_in_a_locked_month_exits_3_naming_the_control_and_the_month(scratch):
    scratch.edit_payroll("2026-01")
    before = scratch.query("SELECT * FROM state.close_state ORDER BY month")
    loaded = scratch.query("SELECT _load_id, count(*) FROM raw.payroll_runs GROUP BY _load_id")

    done = scratch.close(OPEN_MONTH)

    assert done.code == 3, done.stderr
    assert [c["id"] for c in done.json["failed_controls"]] == ["CTL-LOCK"]
    assert "2026-01" in done.json["message"]
    [remediation] = done.json["failed_controls"][0]["remediation"]
    assert remediation.startswith("Month 2026-01 is locked and its inputs changed")
    assert scratch.query("SELECT * FROM state.close_state ORDER BY month") == before
    assert scratch.query("SELECT _load_id, count(*) FROM raw.payroll_runs GROUP BY _load_id") == loaded


def test_an_edit_in_the_locked_month_is_refused_when_closing_that_month_too(scratch):
    scratch.edit_payroll("2026-01")
    done = scratch.close(LOCKED_THROUGH)
    assert done.code == 3, done.stderr
    assert [c["id"] for c in done.json["failed_controls"]] == ["CTL-LOCK"]


def test_an_edit_in_the_open_month_closes_normally(scratch):
    scratch.edit_payroll(OPEN_MONTH)

    done = scratch.close(OPEN_MONTH)

    assert done.code == 0, done.stderr
    assert done.json["close_status"] in ("closed", "unchanged")
    assert scratch.statuses() == {**{m: "locked" for m in LOCKED}, OPEN_MONTH: "closed"}
    [(stored, recorded)] = scratch.query(
        f"SELECT input_digest, (SELECT count(*) FROM ctl.control_results WHERE control_id = 'CTL-LOCK')"
        f" FROM state.close_state WHERE month = '{OPEN_MONTH}'")
    assert stored and recorded > 0


def test_ctl_lock_reports_a_locked_month_whose_loaded_rows_changed(scratch):
    """The control itself, on the loaded rows: a row edited in `raw` after the lock is reported."""
    with db.connect(scratch.warehouse) as con:
        con.execute("UPDATE raw.payroll_runs SET headcount = '99', _row_hash = 'edited'"
                    " WHERE _month = '2026-01' AND half = 'mid'")
        state.define_row_view(con)
        with pytest.raises(ControlFailure) as raised:
            controls.run(con, OPEN_MONTH, "raw")
    [failure] = raised.value.failed_controls
    assert failure["id"] == "CTL-LOCK" and failure["violating_rows"] == 1
    assert "Month 2026-01 is locked" in failure["remediation"][0]
