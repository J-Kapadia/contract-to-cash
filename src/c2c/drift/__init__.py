"""Drift errors: books that change shape, planted in a copy of the books.

An answer-key error (`c2c.inject.INJECT_ERRORS`) edits the row the answer key names. A drift error
needs no answer key, only the books and a month: it makes the books of that month differ from the
months before it, in a way the load must notice (an unmapped ledger account, a bank counterparty
never seen before, a source file with an extra column, a contract line type no billing rule bills).

The registry is this directory. Each `*.py` module next to this file, other than `__init__`, is one
drift error, named by its stem, and defines

    apply(books: c2c.inject.Books, month: str, rng: random.Random) -> list[dict]

which edits the copy through `Books` and `Table` (or rewrites one file of `books.directory` itself)
and returns the changed rows in the shape `Books.save()` returns. Adding an error is adding a module;
no shared file changes. The directory holds no error yet.

`inject_drift` copies the books, applies one error and writes the copy's manifest. The draws come from
`random.Random(f"{seed}:inject:{error}")`, so a copy is the same for the same seed.

Choices made where the brief is silent: the manifest's preset is the one recorded beside the source
books when there is a manifest, else `edited`; a module that cannot be imported is an error of the
drift package, not a skipped entry.
"""

from __future__ import annotations

import importlib
import importlib.util
import random
from pathlib import Path

from ..errors import UsageError
from ..gen.manifest import read as read_manifest
from ..inject import MANIFEST, Books, Injected, _fresh_copy, write_manifest
from ..load import check_in_window

HERE = Path(__file__).parent
EDITED = "edited"


def errors(directory: Path = HERE) -> tuple[str, ...]:
    """The drift errors in `directory`: the sorted stems of its `*.py` modules other than `__init__`."""
    return tuple(sorted(p.stem for p in Path(directory).glob("*.py") if p.stem != "__init__"))


def _module(error: str, directory: Path):
    if Path(directory).resolve() == HERE.resolve():
        return importlib.import_module(f"{__name__}.{error}")
    spec = importlib.util.spec_from_file_location(f"c2c_drift_{error}", Path(directory) / f"{error}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def apply_error(books_dir: Path, error: str, month: str, seed: int, directory: Path = HERE) -> list[dict]:
    """Plant `error` in the books at `books_dir`, in place, and return the rows it changed."""
    if error not in errors(directory):
        raise UsageError(f"{error} is not a drift error in {directory}: one of {', '.join(errors(directory)) or 'none'}.")
    apply = _module(error, directory).apply
    return apply(Books(Path(books_dir)), month, random.Random(f"{seed}:inject:{error}"))


def inject_drift(books: Path, out: Path, error: str, month: str, seed: int, directory: Path = HERE) -> Injected:
    """Copy `books` to `out` and plant the drift error `error` in `month` of the copy."""
    books, out = Path(books), Path(out)
    check_in_window(books, month)
    if error not in errors(directory):
        raise UsageError(f"{error} is not a drift error in {directory}: one of {', '.join(errors(directory)) or 'none'}.")
    _fresh_copy(books, out)
    changed = apply_error(out, error, month, seed, directory)
    source = books.parent / MANIFEST
    preset = read_manifest(source).preset if source.is_file() else EDITED
    return Injected(error, month, changed, write_manifest(books, out, preset, seed))
