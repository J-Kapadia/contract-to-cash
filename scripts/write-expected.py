#!/usr/bin/env python3
"""Write the expected outputs of the committed `mini` books from the Python reference.

    uv run python scripts/write-expected.py [--books DIR] [--truth DIR] [--config DIR] [--out DIR]

Every submodule of `c2c.truth_reference` that defines `expected(books, truth, config)` owns one
reference computation and returns the text of its expected files by file name; this script
collects them all, in module-name order, and writes each file. The numbers come from the
reference alone, in whole cents, never from the warehouse, so a test that compares a close or a
forecast with them compares two implementations. Run it again whenever the books are regenerated.
"""

from __future__ import annotations

import argparse
import importlib.util
import pkgutil
from pathlib import Path

from c2c import truth_reference

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures"


def load(name: str):
    """Execute a submodule without binding it on the package: `rollforward` the module must not replace
    `rollforward` the function that `from c2c import truth_reference as tr` callers use."""
    spec = importlib.util.find_spec(name)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def expected(books: Path, truth: Path, config: Path) -> dict[str, str]:
    """The text of each expected file, by file name, merged over every reference submodule."""
    files: dict[str, str] = {}
    for info in sorted(pkgutil.iter_modules(truth_reference.__path__), key=lambda i: i.name):
        module = load(f"{truth_reference.__name__}.{info.name}")
        if not hasattr(module, "expected"):
            continue
        for name, text in module.expected(books, truth, config).items():
            if name in files:
                raise ValueError(f"{name} is produced by more than one reference")
            files[name] = text
    return files


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--books", type=Path, default=FIXTURES / "books")
    parser.add_argument("--truth", type=Path, default=FIXTURES / "truth")
    parser.add_argument("--config", type=Path, default=FIXTURES / "config")
    parser.add_argument("--out", type=Path, default=FIXTURES / "expected")
    args = parser.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)
    for name, text in expected(args.books, args.truth, args.config).items():
        (args.out / name).write_text(text, encoding="utf-8", newline="\n")
        print(f"wrote {args.out / name}")


if __name__ == "__main__":
    main()
