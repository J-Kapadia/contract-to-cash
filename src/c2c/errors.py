"""The errors a command raises, each carrying the exit code it ends the process with."""

from __future__ import annotations

from collections.abc import Iterable


class C2CError(Exception):
    """A failure with an exit code and a message that tells the user what to do."""

    def __init__(self, exit: int, message: str) -> None:
        super().__init__(message)
        self.exit = exit
        self.message = message


class UsageError(C2CError):
    """Bad invocation: usage, invalid configuration, a month outside the window, a warehouse holding
    different books, an unknown number ID."""

    def __init__(self, message: str) -> None:
        super().__init__(2, message)


class PreconditionError(C2CError):
    """A precondition is not met: sign-off missing or stale, month not closed, no answer key, no books."""

    def __init__(self, message: str) -> None:
        super().__init__(4, message)


class ControlFailure(C2CError):
    """A control or named check failed. `failed_controls` lists each with its violating rows."""

    def __init__(self, message: str, failed_controls: Iterable[dict] = ()) -> None:
        super().__init__(3, message)
        self.failed_controls = list(failed_controls)


class CheckMismatch(C2CError):
    """The `--check` comparison against the answer key found a difference."""

    def __init__(self, message: str) -> None:
        super().__init__(5, message)


class ProvenanceError(C2CError):
    """Provenance is incomplete or a digest does not match."""

    def __init__(self, message: str) -> None:
        super().__init__(6, message)


class InternalError(C2CError):
    """Anything else that went wrong, including a failed dbt build or test."""

    def __init__(self, message: str) -> None:
        super().__init__(70, message)


class LockHeld(C2CError):
    """The warehouse lock is held by another process."""

    def __init__(self, message: str = "another c2c process holds the warehouse") -> None:
        super().__init__(70, message)


# The error that wins when several apply, first to last. Any other error ranks after these.
_PRECEDENCE = (LockHeld, UsageError, PreconditionError, ControlFailure, CheckMismatch, ProvenanceError, InternalError)


def _rank(error: C2CError) -> int:
    for rank, kind in enumerate(_PRECEDENCE):
        if isinstance(error, kind):
            return rank
    return len(_PRECEDENCE)


def precedence(errors: Iterable[C2CError]) -> C2CError | None:
    """The error whose exit code the process ends with, or None when there is none.

    Ties keep the earliest error given."""
    return min(errors, key=_rank, default=None)
