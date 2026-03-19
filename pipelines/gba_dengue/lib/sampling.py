"""Sampling-day helpers.

Translated from GBA ``IdentifySamplingDay.py``.
"""

from __future__ import annotations

import pandas as pd


def get_day_abbreviation(thisdate: str | pd.Timestamp) -> str:
    """Return a pandas weekly frequency string like ``'W-MON'`` for the given date."""
    ts = pd.Timestamp(thisdate)
    return f"W-{ts.strftime('%a').upper()}"
