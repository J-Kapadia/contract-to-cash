"""Run `c2c fake ...` where `fake` is a stand-in command taking the given warehouse lock.

    python fake_command.py <shared|exclusive|none> [c2c flags...]

Prints the seconds the CLI took to stderr on exit; the exit code is the CLI's own.
"""

import sys
import time
import types

import click

from c2c import cli, paths


@click.command(name="fake")
@paths.path_options
def command() -> None:
    click.echo("ran")


mode = sys.argv[1]
cli._module_names = lambda: ["fake"]
cli._load = lambda name: types.SimpleNamespace(command=command, LOCK=None if mode == "none" else mode)

start = time.monotonic()
try:
    cli.main(["fake", *sys.argv[2:]])
finally:
    print(f"elapsed={time.monotonic() - start:.3f}", file=sys.stderr)
