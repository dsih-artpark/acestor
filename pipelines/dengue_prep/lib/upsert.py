"""Shared upsert helper for dengue_prep prepared-data files."""

from __future__ import annotations

import logging
from pathlib import Path

import pandas as pd

log = logging.getLogger(__name__)


def upsert_csv(dest_path: Path, new_df: pd.DataFrame, key_cols: list[str]) -> int:
    """Merge new_df into dest_path, dedup on key_cols (keep latest), write back.

    If dest_path exists, the existing rows are loaded and concatenated with new_df
    before deduplication — so re-running the same date range is idempotent and a
    corrected file always wins over an older one (correction semantics).

    Returns the total number of rows written.
    """
    if dest_path.is_file():
        existing = pd.read_csv(dest_path, low_memory=False)
        existing = existing.dropna(how="all")
        n_existing = len(existing)
        combined = pd.concat([existing, new_df], ignore_index=True)
        log.info(
            "upsert: %s — merging %d new rows with %d existing rows (%d total before dedup)",
            dest_path.name,
            len(new_df),
            n_existing,
            len(combined),
        )
    else:
        combined = new_df.copy()
        log.info(
            "upsert: %s — new file, writing %d rows as initial load",
            dest_path.name,
            len(new_df),
        )
    combined["date"] = pd.to_datetime(combined["date"])
    n_before_dedup = len(combined)
    combined = combined.drop_duplicates(subset=key_cols, keep="last")
    n_dropped = n_before_dedup - len(combined)
    if n_dropped:
        log.info(
            "upsert: %s — deduped on %s: removed %d duplicate row(s) (kept last = "
            "most recent run wins, correction semantics)",
            dest_path.name,
            key_cols,
            n_dropped,
        )
    combined = combined.sort_values(key_cols).reset_index(drop=True)
    dest_path.parent.mkdir(parents=True, exist_ok=True)
    dest_path.write_text(combined.to_csv(index=False))
    log.info("upsert: %s — wrote %d rows", dest_path.name, len(combined))
    return len(combined)
