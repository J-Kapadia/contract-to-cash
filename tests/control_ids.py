"""The control ids a close reports: the files in controls/, by stage and then by file name."""

from __future__ import annotations

from pathlib import Path

from c2c import controls


def expected_ids(stages: tuple[str, ...] = ("raw", "mart"),
                 directory: str | Path = controls.CONTROLS_DIR) -> list[str]:
    paths = sorted(Path(directory).glob("CTL-*.sql"))
    return [path.stem for stage in stages for path in paths if controls._header(path)[1] == stage]


def raw_ids(directory: str | Path = controls.CONTROLS_DIR) -> list[str]:
    return expected_ids(("raw",), directory)


def mart_ids(directory: str | Path = controls.CONTROLS_DIR) -> list[str]:
    return expected_ids(("mart",), directory)
