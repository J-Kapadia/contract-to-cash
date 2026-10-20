"""A source file gains a column: one books file, drawn with the generator, grows an empty `note` column.

The header is extended and every row of the file re-emitted with an empty last field; every other
file stays byte for byte. One change is recorded for the file, under an empty key. The month only
decides nothing here, so it is accepted for uniformity with the other drift errors.
"""

from __future__ import annotations

import random

from ..gen import BOOKS
from ..inject import Books

COLUMN = "note"


def apply(books: Books, month: str, rng: random.Random) -> list[dict]:
    table = books[rng.choice(sorted(BOOKS))]
    table.header_line += f",{COLUMN}"
    table.header.append(COLUMN)
    for position, (row, _) in enumerate(table.rows):
        row[COLUMN] = ""
        table.rows[position][1] = None
    table.dirty = True
    books.changes[(table.path.name, ())] = "columns"
    return books.save()
