"""Typed configuration dataclasses for the dengue_downscale pipeline."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping


@dataclass(frozen=True)
class DownscaleRunConfig:
    source_run_id: str  # a run ID string, or the sentinel "latest"

    @property
    def is_latest(self) -> bool:
        return self.source_run_id == "latest"

    @classmethod
    def from_raw(cls, raw: Mapping[str, Any]) -> DownscaleRunConfig:
        source_run_id = str(raw.get("source_run_id", "")).strip()
        if not source_run_id:
            raise ValueError("run.source_run_id is required in downscale config")
        return cls(source_run_id=source_run_id)


@dataclass(frozen=True)
class DownscaleConfig:
    parent_level: str  # singular, e.g. "district"
    child_level: str  # singular, e.g. "mandal"
    window_weeks: int
    cases_csv: str
    geojson_base_path: str
    on_missing_parents: str = "error"  # "error" | "warn" — parents with no children
    # When the primary window has zero cases across all children, try this
    # longer window before falling back to uniform-split. None = disabled,
    # byte-identical to previous behaviour. See issue #86.
    historical_fallback_weeks: int | None = None

    @property
    def parent_level_plural(self) -> str:
        return self.parent_level + "s"

    @property
    def child_level_plural(self) -> str:
        return self.child_level + "s"

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
        hfw_raw = raw.get("historical_fallback_weeks")
        historical_fallback_weeks: int | None
        if hfw_raw is None:
            historical_fallback_weeks = None
        else:
            historical_fallback_weeks = int(hfw_raw)
            if historical_fallback_weeks <= window_weeks:
                raise ValueError(
                    f"downscale.historical_fallback_weeks "
                    f"({historical_fallback_weeks}) must be > window_weeks "
                    f"({window_weeks}) to be a longer fallback window"
                )
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
            on_missing_parents=_validated_on_missing(
                raw.get("on_missing_parents", "error")
            ),
            historical_fallback_weeks=historical_fallback_weeks,
        )


def _validated_on_missing(value: Any) -> str:
    v = str(value).strip().lower() or "error"
    if v not in ("error", "warn"):
        raise ValueError(
            f"downscale.on_missing_parents must be 'error' or 'warn', got {value!r}"
        )
    return v


@dataclass(frozen=True)
class DownscaleMapsConfig:
    """Static PNG choropleth output for child-level predictions (issue #64).

    Mirrors the dengue pipeline's MapsConfig — one PNG per (week, model,
    threshold) tuple, rendered against the child geojson layer that the
    downscale step already loads.
    """

    enabled: bool
    output_dir: str
    figure_title: str

    @classmethod
    def from_raw(cls, raw: Mapping[str, Any]) -> DownscaleMapsConfig:
        return cls(
            enabled=bool(raw.get("enabled", True)),
            output_dir=str(raw.get("output_dir", "outputs/maps")).strip()
            or "outputs/maps",
            figure_title=str(raw.get("figure_title") or "Dengue risk map").strip()
            or "Dengue risk map",
        )
