"""The `c2c` command line.

Commands are discovered, not registered: every module in `c2c/commands/` exports
`command` (a `click.Command`) and `LOCK`, the warehouse lock the command needs:
`"shared"`, `"exclusive"` or `None`.
"""

from __future__ import annotations

import importlib
import pkgutil
from types import ModuleType

import click

from . import __version__, commands


def _module_names() -> list[str]:
    return sorted(m.name for m in pkgutil.iter_modules(commands.__path__) if not m.name.startswith("_"))


def _load(name: str) -> ModuleType:
    return importlib.import_module(f"{commands.__name__}.{name}")


class CommandGroup(click.Group):
    """A group whose commands are the modules of `c2c.commands`."""

    def list_commands(self, ctx: click.Context) -> list[str]:
        return _module_names()

    def get_command(self, ctx: click.Context, cmd_name: str) -> click.Command | None:
        if cmd_name not in _module_names():
            return None
        return _load(cmd_name).command


def command_lock(name: str) -> str | None:
    """The warehouse lock a command takes: "shared", "exclusive" or None."""
    return _load(name).LOCK


@click.group(cls=CommandGroup)
@click.version_option(version=__version__, prog_name="c2c")
def main() -> None:
    """Contract-to-cash for a synthetic company: books, close, forecast, memo."""
