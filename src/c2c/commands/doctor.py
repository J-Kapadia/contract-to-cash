"""`c2c doctor`: check the installation, the books, the sign-off and the warehouse."""

from __future__ import annotations

import click

from .. import doctor, paths
from ..envelope import Envelope
from ..errors import UsageError

LOCK = "shared"


def _text(rows: list[dict]) -> str:
    width = max(len(r["check"]) for r in rows)
    return "\n".join(f"{r['status']:<4}  {r['check']:<{width}}  {r['detail']}" for r in rows)


@click.command(name="doctor")
@click.option("--links", is_flag=True, help="Check the cited URLs (not available yet).")
@paths.path_options
def command(links: bool) -> None:
    """Report the health of this installation; exit 0 healthy, 1 warnings, 2 failures."""
    if links:
        raise UsageError("doctor --links is not available yet.")
    resolved = click.get_current_context().obj
    rows = doctor.run(resolved)
    code = doctor.exit_code(rows)
    counts = {status: sum(r["status"] == status for r in rows) for status in (doctor.WARN, doctor.FAIL)}
    summary = ("healthy" if code == 0 else
               f"{counts[doctor.FAIL]} failure(s), {counts[doctor.WARN]} warning(s)")
    status = {0: "ok", 1: "warning", 2: "failed"}[code]
    message = f"doctor: {summary}\n{_text(rows)}"
    if resolved.as_json:
        Envelope(command="doctor", status=status, exit=code, extra={"checks": rows}, message=message).render(True)
    else:
        click.echo(message)
    if code:
        click.get_current_context().exit(code)
