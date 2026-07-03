"""Integer apportionment via the Largest Remainder Method (Hamilton's method).

Used at the tail of the downscale step so that each parent's rounded integer
"budget" is distributed across its children as integers that:

  * sum exactly to ``round_half_up(sum(raw_i))`` — the parent's displayable total
  * never exceed ``floor(raw_i) + 1`` per child — off by at most one

Pure function, no I/O. See issue #83.
"""

from __future__ import annotations

import math
from typing import Sequence


def round_half_up(x: float) -> int:
    """Round half-up (0.5 rounds up), clamped to non-negative.

    Chosen over ``ceil`` for the parent target so predictions don't inflate
    on every fractional value (a raw of 1.4 stays 1 instead of becoming 2).
    """
    return int(math.floor(max(float(x), 0.0) + 0.5))


def largest_remainder(
    raw_predictions: Sequence[float],
    region_ids: Sequence[str],
    recent_cases: Sequence[float] | None = None,
) -> list[int]:
    """Distribute ``round_half_up(sum(raw_predictions))`` integer units across children.

    Algorithm (Largest Remainder Method / Hamilton's method):
      1. ``target = round_half_up(sum(raw))``
      2. Each child gets a base allocation of ``floor(raw_i)``
      3. ``leftover = target - sum(base)`` — always in ``[0, n_children]``
      4. Rank children by fractional remainder ``(raw_i - floor(raw_i))``
         descending. Tie-breaks (in order):
           a. higher recent-cases wins — direct signal of on-the-ground
              intensity; ties on remainder are resolved toward the ward with
              more real recent cases (per Prerna's ask in issue #86).
           b. lower region_id (lexicographic) wins — deterministic tail
      5. The top ``leftover`` children get ``+1``

    Args:
        raw_predictions: per-child raw prediction floats. Must be finite and
            non-negative.
        region_ids: aligned with ``raw_predictions``; used for deterministic
            secondary tie-break.
        recent_cases: aligned with ``raw_predictions``; used for primary
            tie-break when fractional remainders are equal. When ``None``,
            defaults to zero for every child (falls through to region_id
            tie-break).

    Returns:
        list of integer allocations, aligned with the input order. Sum equals
        ``round_half_up(sum(raw_predictions))``; each entry equals
        ``floor(raw_i)`` or ``floor(raw_i) + 1``.

    Raises:
        ValueError: on length mismatch, on negative or non-finite values.

    Examples:
        >>> largest_remainder([0.4, 0.3, 0.2, 0.1], ["a", "b", "c", "d"])
        [1, 0, 0, 0]
        >>> largest_remainder([0.7, 0.7], ["a", "b"])
        [1, 0]
        >>> largest_remainder([0.0, 0.0, 0.0], ["a", "b", "c"])
        [0, 0, 0]
    """
    if len(raw_predictions) != len(region_ids):
        raise ValueError(
            f"largest_remainder: length mismatch: "
            f"{len(raw_predictions)} raw vs {len(region_ids)} region_ids"
        )
    if recent_cases is not None and len(recent_cases) != len(raw_predictions):
        raise ValueError(
            f"largest_remainder: length mismatch: "
            f"{len(recent_cases)} recent_cases vs {len(raw_predictions)} raw"
        )
    if not raw_predictions:
        return []

    values = [float(v) for v in raw_predictions]
    cases = (
        [float(c) for c in recent_cases]
        if recent_cases is not None
        else [0.0] * len(values)
    )

    for i, v in enumerate(values):
        if not math.isfinite(v):
            raise ValueError(
                f"largest_remainder: non-finite value at index {i} "
                f"(region={region_ids[i]!r}): {v}"
            )
        if v < 0.0:
            raise ValueError(
                f"largest_remainder: negative value at index {i} "
                f"(region={region_ids[i]!r}): {v}"
            )

    target = round_half_up(sum(values))
    base = [math.floor(v) for v in values]
    leftover = target - sum(base)

    if leftover == 0:
        return base

    # Rank by (remainder desc, recent_cases desc, region_id asc).
    remainders = [
        (v - math.floor(v), cases[i], region_ids[i], i) for i, v in enumerate(values)
    ]
    remainders.sort(key=lambda t: (-t[0], -t[1], t[2]))

    for rem_tuple in remainders[:leftover]:
        idx = rem_tuple[3]
        base[idx] += 1

    return base
