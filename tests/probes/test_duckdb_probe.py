"""What the warehouse relies on from DuckDB and the OS, checked against the real thing."""

from __future__ import annotations

import json
import re
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from c2c import db, lock
from conftest import ROOT

FAKE = Path(__file__).with_name("fake_command.py")


def test_uncommitted_rows_are_invisible_to_a_second_connection(tmp_path):
    path = tmp_path / "w.duckdb"
    writer = db.connect(path)
    writer.execute("CREATE TABLE t (x INTEGER)")
    writer.execute("INSERT INTO t VALUES (1)")
    reader = db.connect(path)

    writer.execute("BEGIN")
    writer.execute("INSERT INTO t VALUES (2)")
    assert writer.execute("SELECT count(*) FROM t").fetchone() == (2,)
    assert reader.execute("SELECT count(*) FROM t").fetchone() == (1,)

    writer.execute("COMMIT")
    assert reader.execute("SELECT count(*) FROM t").fetchone() == (2,)
    writer.close()
    reader.close()


READER = textwrap.dedent("""
    import sys, time
    from pathlib import Path
    from c2c import db

    path, mine, theirs = sys.argv[1:]
    con = db.connect(path, read_only=True)
    Path(mine).touch()
    deadline = time.monotonic() + 20
    while not Path(theirs).exists():  # leave only once the other process has the file open too
        if time.monotonic() > deadline:
            sys.exit("the other reader never opened the file")
        time.sleep(0.02)
    print(con.execute("SELECT count(*) FROM t").fetchone()[0])
""")


def test_two_read_only_processes_open_the_same_file(tmp_path):
    path = tmp_path / "w.duckdb"
    con = db.connect(path)
    con.execute("CREATE TABLE t AS SELECT range AS x FROM range(5)")
    con.close()

    ready = [tmp_path / "ready-a", tmp_path / "ready-b"]
    procs = [
        subprocess.Popen([sys.executable, "-c", READER, str(path), str(mine), str(theirs)],
                         cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        for mine, theirs in (ready, ready[::-1])
    ]
    for proc in procs:
        out, err = proc.communicate(timeout=60)
        assert proc.returncode == 0, err
        assert out.strip() == "5"


def run_fake(mode: str, warehouse: Path, *flags: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(FAKE), mode, "--warehouse", str(warehouse), *flags],
                          cwd=ROOT, capture_output=True, text=True, timeout=60, check=False)


def elapsed(proc: subprocess.CompletedProcess) -> float:
    return float(re.search(r"elapsed=([0-9.]+)", proc.stderr).group(1))


@pytest.fixture
def holder(tmp_path):
    """Start a process holding the lock on the warehouse in the given mode; it lets go when the test ends."""
    started = []

    def hold(mode: str) -> Path:
        warehouse = tmp_path / "w.duckdb"
        code = ("import sys; from c2c import lock\n"
                f"with lock.held(sys.argv[1], {mode!r}):\n"
                "    print('held', flush=True); sys.stdin.read()")
        proc = subprocess.Popen([sys.executable, "-c", code, str(warehouse)], cwd=ROOT, text=True,
                                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        started.append(proc)
        assert proc.stdout.readline().strip() == "held", proc.stderr.read()
        return warehouse

    yield hold
    for proc in started:
        proc.stdin.close()
        proc.wait(timeout=30)


def test_a_held_exclusive_lock_makes_a_second_writer_exit_70_quickly(holder):
    warehouse = holder("exclusive")
    proc = run_fake("exclusive", warehouse, "--json")
    assert proc.returncode == 70
    assert elapsed(proc) < 1
    envelope = json.loads(proc.stdout)
    assert envelope["exit"] == 70
    assert envelope["message"] == "another c2c process holds the warehouse"


def test_a_held_exclusive_lock_also_refuses_a_reader(holder):
    assert run_fake("shared", holder("exclusive")).returncode == 70


def test_a_held_shared_lock_refuses_a_writer_but_not_another_reader(holder):
    warehouse = holder("shared")
    assert run_fake("shared", warehouse).returncode == 0
    assert run_fake("exclusive", warehouse).returncode == 70


def test_a_command_without_a_lock_ignores_a_held_one(holder):
    assert run_fake("none", holder("exclusive")).returncode == 0


def test_the_lock_is_released_when_the_process_ends(tmp_path):
    warehouse = tmp_path / "w.duckdb"
    assert run_fake("exclusive", warehouse).returncode == 0
    assert run_fake("exclusive", warehouse).returncode == 0
    with lock.held(warehouse, "exclusive"):
        pass


def test_the_lock_is_on_the_resolved_warehouse(holder, tmp_path):
    warehouse = holder("exclusive")
    other = tmp_path / "other.duckdb"
    assert run_fake("exclusive", other).returncode == 0
    assert run_fake("exclusive", warehouse).returncode == 70
