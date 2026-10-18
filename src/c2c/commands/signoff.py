"""`c2c signoff`: a person signs the reviewed-match file and the policy file now in force."""

from __future__ import annotations

import sys
from datetime import date

import click

from .. import paths, signoff
from ..envelope import Envelope
from ..errors import PreconditionError

LOCK = None


@click.command(name="signoff")
@click.option("--signer", required=True, help="Name of the person signing.")
@click.option("--role", required=True, help="Role of the signer, e.g. finance_lead.")
@click.option("--config", help="Configuration directory (C2C_CONFIG_DIR).")
@click.option("--json", "as_json", is_flag=True, help="Machine output on stdout.")
def command(signer: str, role: str, config: str | None, as_json: bool) -> None:
    """Sign the files in the configuration directory. Needs a terminal: a person confirms it."""
    if not sys.stdin.isatty():
        raise PreconditionError("signoff needs a terminal: a person must read the hashes and confirm. "
                                "Run `c2c signoff` in an interactive shell.")
    resolved = paths.resolve(config=config, as_json=as_json)
    matches, policy, combined = signoff.hashes(resolved.config)
    click.echo(f"matches  {matches}\npolicy   {policy}\ncombined {combined}", err=True)
    if not click.confirm(f"Sign {resolved.config} as {signer} ({role})?", err=True):
        raise click.Abort()
    path = signoff.write_for(resolved.config, resolved.signoff, signer, role, date.today().isoformat())
    Envelope(command="signoff", status="ok", exit=0, message=f"signed: wrote {path}",
             extra={"combined_sha256": combined}).render(as_json)
