"""Fresh-write helper for dengue_prep prepared-data files."""

from __future__ import annotations

import logging
from pathlib import Path

import pandas as pd

log = logging.getLogger(__name__)


def write_csv(dest_path: Path, new_df: pd.DataFrame, key_cols: list[str]) -> int:
    """Write new_df to dest_path, replacing any existing file entirely.

    Each pipeline run starts from a clean slate — no old rows are carried forward.
    This ensures that config changes (e.g. updated filters) are fully reflected in
    the output without stale data from previous runs bleeding through.

    Returns the total number of rows written.
    """
    df = new_df.copy()
    df["date"] = pd.to_datetime(df["date"])
    n_before_dedup = len(df)
    df = df.drop_duplicates(subset=key_cols, keep="last")
    n_dropped = n_before_dedup - len(df)
    if n_dropped:
        log.info(
            "write_csv: %s — deduped on %s: removed %d duplicate row(s)",
            dest_path.name,
            key_cols,
            n_dropped,
        )
    df = df.sort_values(key_cols).reset_index(drop=True)
    dest_path.parent.mkdir(parents=True, exist_ok=True)
    dest_path.write_text(df.to_csv(index=False))
    log.info("write_csv: %s — wrote %d rows", dest_path.name, len(df))
    return len(df)
