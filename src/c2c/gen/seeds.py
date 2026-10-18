"""Seeded random streams for the generator.

Every random choice draws from its own stream, keyed by the run seed, the entity it is
about and the purpose of the draw. A stream does not depend on how many other draws were
made or in what order, so adding a customer or an attribute never shifts the others.
"""

from __future__ import annotations

import random


def rng(seed: int, entity: str, purpose: str) -> random.Random:
    """The stream for `purpose` of `entity` under `seed`: `Random(f"{seed}:{entity}:{purpose}")`."""
    return random.Random(f"{seed}:{entity}:{purpose}")
