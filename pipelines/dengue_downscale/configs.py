"""Typed configuration dataclasses for the dengue_downscale pipeline."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping


@dataclass(frozen=True)
class DownscaleRunConfig:
    source_run_id: str

    @classmethod
    def from_raw(cls, raw: Mapping[str, Any]) -> DownscaleRunConfig:
        source_run_id = str(raw.get("source_run_id", "")).strip()
        if not source_run_id:
            raise ValueError("run.source_run_id is required in downscale config")
        return cls(source_run_id=source_run_id)


@dataclass(frozen=True)
class DownscaleConfig:
    parent_level: str
    child_level: str
    window_weeks: int
    cases_csv: str
    geojson_base_path: str

    @classmethod
    def from_raw(cls, raw: Mapping[str, Any]) -> DownscaleConfig:
        parent_level = str(raw.get("parent_level", "")).strip()
        child_level = str(raw.get("child_level", "")).strip()
        if not parent_level:
            raise ValueError("downscale.parent_level is required")
        if not child_level:
            raise ValueError("downscale.child_level is required")
        if parent_level == child_level:
            raise ValueError(
                f"downscale.parent_level and child_level must differ, got {parent_level!r} for both"
            )
        window_weeks = int(raw.get("window_weeks", 4))
        if window_weeks <= 0:
            raise ValueError(f"downscale.window_weeks must be > 0, got {window_weeks}")
        return cls(
            parent_level=parent_level,
            child_level=child_level,
            window_weeks=window_weeks,
            cases_csv=str(
                raw.get("cases_csv", "prepared_data/mandal/cases_daily.csv")
            ).strip(),
            geojson_base_path=str(
                raw.get("geojson_base_path", "ap_datasets/geojsons/geojsons_AP")
            ).strip(),
        )
