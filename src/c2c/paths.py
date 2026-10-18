"""Where the books, the answer key, the warehouse and the configuration live.

Each path resolves flag, then environment variable, then default. `--fixture NAME`
moves the defaults of books, truth and warehouse to that fixture; an explicit flag
or variable still wins.
"""

from __future__ import annotations

import functools
import os
from dataclasses import dataclass
from pathlib import Path

import click

# fixture -> (books, truth, warehouse)
FIXTURES = {
    "mini": ("tests/fixtures/books", "tests/fixtures/truth", "warehouse/mini.duckdb"),
    "small": ("data/books/small", "data/truth/small", "warehouse/small.duckdb"),
}
DEFAULT_BOOKS = "data/books"
DEFAULT_TRUTH = "data/truth"
DEFAULT_WAREHOUSE = "warehouse/c2c.duckdb"
DEFAULT_CONFIG = "config"
DEFAULT_SIGNOFF = "config/signoff"


@dataclass(frozen=True)
class Paths:
    books: Path
    truth: Path
    warehouse: Path
    config: Path
    signoff: Path
    fixture: str | None = None
    as_json: bool = False


def resolve(
    *,
    books: str | None = None,
    truth: str | None = None,
    warehouse: str | None = None,
    config: str | None = None,
    fixture: str | None = None,
    as_json: bool = False,
    env: dict[str, str] | None = None,
) -> Paths:
    env = os.environ if env is None else env
    fx_books, fx_truth, fx_warehouse = FIXTURES[fixture] if fixture else (DEFAULT_BOOKS, DEFAULT_TRUTH, DEFAULT_WAREHOUSE)

    def pick(flag: str | None, var: str, default: str) -> Path:
        return Path(flag or env.get(var) or default)

    return Paths(
        books=pick(books, "C2C_BOOKS", fx_books),
        truth=pick(truth, "C2C_TRUTH", fx_truth),
        warehouse=pick(warehouse, "C2C_WAREHOUSE", fx_warehouse),
        config=pick(config, "C2C_CONFIG_DIR", DEFAULT_CONFIG),
        signoff=pick(None, "C2C_SIGNOFF_DIR", DEFAULT_SIGNOFF),
        fixture=fixture,
        as_json=as_json,
    )


_OPTIONS = (
    click.option("--books", help="Books directory (C2C_BOOKS)."),
    click.option("--truth", help="Answer-key directory (C2C_TRUTH)."),
    click.option("--warehouse", help="Warehouse file (C2C_WAREHOUSE)."),
    click.option("--fixture", type=click.Choice(sorted(FIXTURES)),
                 help="Fixture preset: sets the default books, truth and warehouse."),
    click.option("--config", help="Configuration directory (C2C_CONFIG_DIR)."),
    click.option("--json", "as_json", is_flag=True, help="Machine output on stdout."),
)


def path_options(fn):
    """Add the shared path flags and `--json`; the resolved `Paths` is stored on `ctx.obj`."""

    @functools.wraps(fn)
    def wrapper(*args, books, truth, warehouse, fixture, config, as_json, **kwargs):
        ctx = click.get_current_context()
        ctx.obj = resolve(books=books, truth=truth, warehouse=warehouse, config=config,
                          fixture=fixture, as_json=as_json)
        return fn(*args, **kwargs)

    for option in reversed(_OPTIONS):
        wrapper = option(wrapper)
    return wrapper
