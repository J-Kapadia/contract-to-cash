"""A bank counterparty nobody has seen: one receipt of the month is paid under a new payer name.

The receipt is drawn from the month's receipts that a payment row applies; its `counterparty_text`
becomes the first name of `c2c.inject.UNKNOWN_PAYERS` (in draw order) that no books row carries. Its
`reference_text` and the payment row stay, so the receipt still applies to its invoice and only the
payer is new. One change is recorded, the receipt's.
"""

from __future__ import annotations

import random

from ..errors import PreconditionError
from ..gen import BOOKS
from ..inject import UNKNOWN_PAYERS, Books


def apply(books: Books, month: str, rng: random.Random) -> list[dict]:
    bank = books["bank_transactions"]
    applied = {row["bank_txn_id"] for row, _ in books["payments"].rows}
    opening = min((row["posted_on"], row["bank_txn_id"]) for row, _ in bank.rows)[1]
    pool = sorted(row["bank_txn_id"] for row, _ in bank.rows
                  if int(row["amount_cents"]) > 0 and row["posted_on"][:7] == month
                  and row["bank_txn_id"] in applied and row["bank_txn_id"] != opening)
    if not pool:
        raise PreconditionError(f"no receipt in {month} is applied by a payment row: new_counterparty needs one to rename.")
    carried = {value for stem in BOOKS for row, _ in books[stem].rows for value in row.values()}
    names = list(UNKNOWN_PAYERS)
    rng.shuffle(names)
    free = next((name for name in names if name not in carried), None)
    if free is None:
        raise PreconditionError("every name in UNKNOWN_PAYERS is already in the books.")
    bank_txn_id = rng.choice(pool)
    bank.set(bank.where(bank_txn_id=bank_txn_id)[0], counterparty_text=free)
    return books.save()
