"""A ledger account the reference never mapped: one expense line of the month moves to a new account.

The line is drawn from the month's ledger lines on accounts 6000 to 6999, so no tie-out moves. Its
`gl_account` becomes the first of 6900, 6910, 6920, ... that no ledger line uses; the reference lists
no 69xx account. One change is recorded, the line's.
"""

from __future__ import annotations

import random

from ..errors import PreconditionError
from ..inject import Books


def apply(books: Books, month: str, rng: random.Random) -> list[dict]:
    ledger = books["gl"]
    pool = [position for position, (row, _) in enumerate(ledger.rows)
            if row["gl_period"] == month and 6000 <= int(row["gl_account"]) <= 6999]
    if not pool:
        raise PreconditionError(f"no ledger line of {month} is on an expense account (6000 to 6999): unmapped_account needs one to move.")
    used = {row["gl_account"] for row, _ in ledger.rows}
    account = next(str(number) for number in range(6900, 7000, 10) if str(number) not in used)
    ledger.set(rng.choice(pool), gl_account=account)
    return books.save()
