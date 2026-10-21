"""The `c2c` command line.

Commands are discovered, not registered: every module in `c2c/commands/` exports
`command` (a `click.Command`) and `LOCK`, the warehouse lock the command needs:
`"shared"`, `"exclusive"` or `None`. The group takes that lock on the resolved warehouse before the
command runs, and turns what the command raises into the envelope and exit code.
"""

from __future__ import annotations

import copy
import importlib
import pkgutil
import sys
import traceback
from types import ModuleType
from typing import Any

import click

from . import __version__, commands, errors, lock, paths
from .envelope import Envelope


def _module_names() -> list[str]:
    return sorted(m.name for m in pkgutil.iter_modules(commands.__path__) if not m.name.startswith("_"))


def _module(name: str) -> str:
    """The module of a command name: a hyphen in the name is an underscore in the module."""
    return name.replace("-", "_")


def _load(name: str) -> ModuleType:
    return importlib.import_module(f"{commands.__name__}.{_module(name)}")


def _locked(module: ModuleType) -> click.Command:
    """The module's command, taking its warehouse lock around its own `invoke`.

    The warehouse is resolved from the command's own `--warehouse` and `--fixture` (then the
    environment), so the lock is on the file the command will open."""
    cmd = copy.copy(module.command)
    mode = module.LOCK
    run = cmd.invoke

    def invoke(ctx: click.Context) -> Any:
        warehouse = paths.resolve(warehouse=ctx.params.get("warehouse"), fixture=ctx.params.get("fixture")).warehouse
        with lock.held(warehouse, mode):
            return run(ctx)

    cmd.invoke = invoke  # type: ignore[method-assign]
    return cmd


class CommandGroup(click.Group):
    """A group whose commands are the modules of `c2c.commands`."""

    def parse_args(self, ctx: click.Context, args: list[str]) -> list[str]:
        # A failure can happen before any command has parsed its own flags; --json is still honoured.
        ctx.meta["c2c.json"] = "--json" in args
        return super().parse_args(ctx, args)

    def invoke(self, ctx: click.Context) -> Any:
        try:
            return super().invoke(ctx)
        except (click.exceptions.Exit, click.exceptions.Abort):
            raise
        except click.ClickException as exc:
            error: errors.C2CError = errors.UsageError(exc.format_message())
        except errors.C2CError as exc:
            error = exc
        except Exception as exc:
            traceback.print_exc()
            error = errors.InternalError(traceback.format_exception_only(type(exc), exc)[-1].strip())
        command = ctx.invoked_subcommand or "c2c"
        as_json = bool(ctx.meta.get("c2c.json")) or getattr(ctx.obj, "as_json", False)
        Envelope.from_error(command, error).render(as_json)
        ctx.exit(error.exit)

    def list_commands(self, ctx: click.Context) -> list[str]:
        return [name.replace("_", "-") for name in _module_names()]

    def get_command(self, ctx: click.Context, cmd_name: str) -> click.Command | None:
        if _module(cmd_name) not in _module_names():
            return None
        return _locked(_load(cmd_name))


def command_lock(name: str) -> str | None:
    """The warehouse lock a command takes: "shared", "exclusive" or None."""
    return _load(name).LOCK


@click.group(cls=CommandGroup)
@click.version_option(version=__version__, prog_name="c2c")
def main() -> None:
    """Contract-to-cash for a synthetic company: books, close, forecast, memo."""
