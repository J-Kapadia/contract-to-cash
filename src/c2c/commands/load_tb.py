"""`c2c load-tb`: stack a directory of monthly trial balance exports into one mart and check it."""

from __future__ import annotations

import click

from .. import paths
from ..intake import load_tb

LOCK = "exclusive"


@click.command(name="load-tb")
@click.argument("directory")
@click.option("--csv", "csv_path", help="CSV to write (default: fct_trial_balance.csv beside the warehouse file).")
@paths.path_options
def command(directory: str, csv_path: str | None) -> None:
    """Stack the monthly trial balance exports in DIRECTORY into mart.fct_trial_balance and check them."""
    resolved = click.get_current_context().obj
    csv_path = csv_path or str(resolved.warehouse.parent / load_tb.CSV_NAME)
    load_tb.load(resolved, directory, csv_path).render(resolved.as_json)
