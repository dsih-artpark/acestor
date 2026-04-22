"""Shared upsert helper for dengue_prep prepared-data files."""

from __future__ import annotations

from pathlib import Path

import pandas as pd


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
        combined = pd.concat([existing, new_df], ignore_index=True)
    else:
        combined = new_df.copy()
    combined["date"] = pd.to_datetime(combined["date"])
    combined = combined.drop_duplicates(subset=key_cols, keep="last")
    combined = combined.sort_values(key_cols).reset_index(drop=True)
    dest_path.parent.mkdir(parents=True, exist_ok=True)
    dest_path.write_text(combined.to_csv(index=False))
    return len(combined)
