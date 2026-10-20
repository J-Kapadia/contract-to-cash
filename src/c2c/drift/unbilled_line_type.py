"""A contract line type no billing rule bills: one contract line of the month becomes `services`.

The line is drawn from the contract lines known in the month; when none was, from the lines of the
latest version known on or before the month's end of each contract. Its `product` becomes
`services`. One change is recorded, the line's.
"""

from __future__ import annotations

import random

from ..errors import PreconditionError
from ..inject import Books

UNBILLED = "services"


def apply(books: Books, month: str, rng: random.Random) -> list[dict]:
    lines = books["contract_lines"]
    known = [(position, row) for position, (row, _) in enumerate(lines.rows) if row["known_on"][:7] == month]
    if known:
        pool = [position for position, _ in known]
    else:
        through = [(position, row) for position, (row, _) in enumerate(lines.rows) if row["known_on"][:7] <= month]
        latest: dict[str, int] = {}
        for _, row in through:
            latest[row["contract_id"]] = max(latest.get(row["contract_id"], 0), int(row["version"]))
        pool = [position for position, row in through if int(row["version"]) == latest[row["contract_id"]]]
    if not pool:
        raise PreconditionError(f"no contract line is known on or before the end of {month}: unbilled_line_type needs one to change.")
    lines.set(rng.choice(pool), product=UNBILLED)
    return books.save()
