"""The result a command reports: a versioned JSON envelope on stdout, plain text on stderr.

The envelope only ever grows: a field is added, never renamed or removed, and `schema_version`
changes only if that stops being true. The schema is `schemas/envelope.schema.json`.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from json import dumps
from pathlib import Path
from typing import Any

from .errors import C2CError, ControlFailure

SCHEMA_VERSION = 1
SCHEMA_PATH = Path(__file__).parent / "schemas" / "envelope.schema.json"


@dataclass
class Envelope:
    command: str
    status: str  # "ok", "warning" or "failed"
    exit: int
    month: str | None = None
    controls: list[dict] = field(default_factory=list)
    result_hash: str | None = None
    failed_controls: list[dict] = field(default_factory=list)
    message: str | None = None
    schema_version: int = SCHEMA_VERSION
    # Fields only some commands report (`as_of`, `cases`, `alerts`, ...), merged at the top level.
    extra: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_error(cls, command: str, error: C2CError, month: str | None = None) -> Envelope:
        failed = error.failed_controls if isinstance(error, ControlFailure) else []
        return cls(command=command, status="failed", exit=error.exit, month=month,
                   failed_controls=failed, message=error.message)

    def to_dict(self) -> dict:
        """The envelope as a plain dict; optional fields that are unset are left out."""
        out: dict[str, Any] = {"schema_version": self.schema_version, "command": self.command,
                               "status": self.status, "exit": self.exit}
        if self.month is not None:
            out["month"] = self.month
        out["controls"] = self.controls
        if self.result_hash is not None:
            out["result_hash"] = self.result_hash
        if self.failed_controls:
            out["failed_controls"] = self.failed_controls
        if self.message is not None:
            out["message"] = self.message
        out.update(self.extra)
        return out

    def text(self) -> str:
        """The human rendering: the message, then each failed control with how to fix it."""
        lines = [self.message] if self.message else [f"{self.command}: {self.status}"]
        for control in self.failed_controls:
            lines.append(f"{control['id']}: {control['violating_rows']} violating row(s)")
            lines.extend(f"  - {step}" for step in control.get("remediation", []))
        return "\n".join(lines)

    def render(self, json: bool) -> None:
        """With `json`, the envelope on stdout and the human text on stderr; otherwise the text on stderr."""
        if json:
            sys.stdout.write(dumps(self.to_dict(), sort_keys=True) + "\n")
            sys.stdout.flush()
        sys.stderr.write(self.text() + "\n")
        sys.stderr.flush()
