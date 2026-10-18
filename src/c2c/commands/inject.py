"""`c2c inject`: copy the books and plant one named error in the copy."""

from __future__ import annotations

from pathlib import Path

import click

from .. import paths
from ..envelope import Envelope
from ..errors import PreconditionError, UsageError
from ..gen.plan import INJECT_ERRORS
from ..inject import inject

LOCK = None

DEFAULT_SEED = 1


@click.command(name="inject")
@click.option("--error", type=click.Choice(INJECT_ERRORS), help="The error to plant.")
@click.option("--all", "every_error", is_flag=True, help="Plant every error in one copy (not available yet).")
@click.option("--books", help="Books to copy (C2C_BOOKS).")
@click.option("--truth", help="Answer-key directory naming the target rows; default: the books' sibling `truth`.")
@click.option("--out", required=True, help="Directory the changed copy is written to; it must be empty or absent.")
@click.option("--seed", type=int, default=DEFAULT_SEED, show_default=True, help="Seed for the draws that pick the details.")
@click.option("--fixture", type=click.Choice(["mini"]), help="Fixture preset: sets the default books and truth.")
@click.option("--json", "as_json", is_flag=True, help="Machine output on stdout.")
def command(error: str | None, every_error: bool, books: str | None, truth: str | None, out: str, seed: int,
            fixture: str | None, as_json: bool) -> None:
    """Copy the books to OUT with one named error planted in them."""
    if every_error:
        raise UsageError("inject --all is not available; plant one error at a time with --error.")
    if error is None:
        raise UsageError(f"--error is required: one of {', '.join(INJECT_ERRORS)}.")
    resolved = paths.resolve(books=books, truth=truth, fixture=fixture, as_json=as_json)
    books_dir = resolved.books
    if not books_dir.is_dir():
        raise PreconditionError(f"no books at {books_dir}: generate them first, or name them with --books.")
    truth_dir = Path(truth) if truth else books_dir.parent / "truth"
    if not truth and not truth_dir.is_dir():
        truth_dir = resolved.truth

    done = inject(books_dir, truth_dir, Path(out), error, seed)
    Envelope(command="inject", status="ok", exit=0, month=done.month,
             message=f"wrote {out} with {error} planted: {len(done.changed)} row(s) changed.",
             extra={"error": error, "seed": seed, "out": str(out), "manifest": str(done.manifest),
                    "changed": done.changed}).render(as_json)
