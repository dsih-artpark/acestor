"""Filesystem weather source.

Reads pre-prepared CSV files from a local directory. Intended for manual file
drops or Docker volume mounts — the operator writes the files, the pipeline
reads them as-is.

should_persist = False: the step skips incremental fetch/write logic entirely
and references the source files directly. No unit normalization is applied
(files are expected to already be in the pipeline's canonical units).
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Mapping

import pandas as pd

from pipelines.dengue_prep.lib.weather_sources import WeatherSource

log = logging.getLogger(__name__)


class Source(WeatherSource):
    # Source files are used directly — no intermediate write needed.
    should_persist = False

    def get_weather_all_regions(
        self,
        start_date: str,
        end_date: str,
        region_type: str,
        config: Mapping[str, Any],
    ) -> list[dict[str, Any]]:
        """Read all CSVs from source_path and return records within the date range."""
        source_path = str(config.get("source_path", "")).strip()
        if not source_path:
            raise ValueError("filesystem source requires source_path in config")

        base = Path(source_path)
        csv_files = sorted(f for f in base.rglob("*.csv") if not f.name.startswith("."))

        if not csv_files:
            log.warning("filesystem: no CSV files found under %s", source_path)
            return []

        dfs: list[pd.DataFrame] = []
        for f in csv_files:
            try:
                dfs.append(pd.read_csv(f, low_memory=False))
            except Exception:
                log.warning("filesystem: skipping unreadable file %s", f)

        if not dfs:
            return []

        combined = pd.concat(dfs, ignore_index=True)

        if "date" not in combined.columns and "time" in combined.columns:
            combined.rename(columns={"time": "date"}, inplace=True)

        combined["date"] = pd.to_datetime(combined["date"]).dt.strftime("%Y-%m-%d")

        start = pd.Timestamp(start_date).strftime("%Y-%m-%d")
        end = pd.Timestamp(end_date).strftime("%Y-%m-%d")
        combined = combined[(combined["date"] >= start) & (combined["date"] <= end)]

        return combined.to_dict("records")
