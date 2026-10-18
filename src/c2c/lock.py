"""The warehouse lock: one non-blocking `flock` on `<warehouse>.lock` per command.

Readers share it, writers hold it exclusively. The kernel drops it when the process ends, so a
killed run leaves nothing to clean up.
"""

from __future__ import annotations

import fcntl
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from .errors import LockHeld

MODES = {"shared": fcntl.LOCK_SH, "exclusive": fcntl.LOCK_EX}


def lock_path(warehouse: str | Path) -> Path:
    warehouse = Path(warehouse)
    return warehouse.with_name(warehouse.name + ".lock")


@contextmanager
def held(warehouse: str | Path, mode: str | None) -> Iterator[None]:
    """Hold the lock on `warehouse` for the block; `mode` is "shared", "exclusive" or None (no lock).

    Raises `LockHeld` at once if another process holds a conflicting lock."""
    if mode is None:
        yield
        return
    flag = MODES[mode]
    path = lock_path(warehouse)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a") as handle:
        try:
            fcntl.flock(handle, flag | fcntl.LOCK_NB)
        except BlockingIOError:
            raise LockHeld() from None
        yield
