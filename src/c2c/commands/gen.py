"""`c2c gen`: generate the books and the answer key for a fixture, or verify them against the manifest."""

from __future__ import annotations

import json
import sys

import click

from ..gen import generate, verify

LOCK = None

DEFAULT_SEED = 1
DEFAULT_OUT = "data"
CHECK_ID = "CHK-GEN-VERIFY"


def _emit(as_json: bool, envelope: dict, text: str) -> None:
    if as_json:
        click.echo(json.dumps(envelope, sort_keys=True))
        click.echo(text, err=True)
    else:
        click.echo(text)


@click.command(name="gen")
@click.option("--seed", type=int, default=DEFAULT_SEED, show_default=True, help="Seed for every random draw.")
@click.option("--out", default=DEFAULT_OUT, show_default=True,
              help="Root directory; books go to OUT/books/<fixture>, the answer key to OUT/truth/<fixture>.")
@click.option("--fixture", type=click.Choice(["small"]), help="Fixture preset to generate or verify.")
@click.option("--verify", "do_verify", is_flag=True, help="Check the files against the manifest instead of writing.")
@click.option("--json", "as_json", is_flag=True, help="Machine output on stdout.")
def command(seed: int, out: str, fixture: str | None, do_verify: bool, as_json: bool) -> None:
    """Generate books and truth, or verify them against the manifest."""
    if fixture is None:
        raise click.UsageError("name a fixture with --fixture small; only fixtures can be generated so far.")

    if not do_verify:
        built = generate(fixture, seed, out)
        _emit(as_json,
              {"schema_version": 1, "command": "gen", "status": "ok", "exit": 0, "preset": fixture,
               "seed": seed, "out": out, "files": len(built.files)},
              f"wrote {len(built.files)} files for {fixture} (seed {seed}) under {out}")
        return

    try:
        drifted = verify(out, fixture)[fixture]
    except FileNotFoundError as exc:
        _emit(as_json,
              {"schema_version": 1, "command": "gen", "status": "failed", "exit": 4, "message": str(exc)},
              f"{exc}; run `c2c gen --fixture {fixture}` first.")
        sys.exit(4)
    if drifted:
        _emit(as_json,
              {"schema_version": 1, "command": "gen", "status": "failed", "exit": 3,
               "failed_checks": [{"id": CHECK_ID, "drifted_files": drifted}],
               "message": f"{len(drifted)} file(s) differ from the manifest"},
              f"{CHECK_ID}: {len(drifted)} file(s) differ from the manifest: {', '.join(drifted)}. "
              f"Regenerate with `c2c gen --fixture {fixture}`.")
        sys.exit(3)
    _emit(as_json,
          {"schema_version": 1, "command": "gen", "status": "ok", "exit": 0, "preset": fixture,
           "checks": [{"id": CHECK_ID, "status": "pass"}]},
          f"{CHECK_ID}: {fixture} books match the manifest")
