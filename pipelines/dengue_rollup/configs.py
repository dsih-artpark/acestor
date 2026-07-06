"""Typed configuration dataclasses for the dengue_rollup pipeline."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping


@dataclass(frozen=True)
class RollupRunConfig:
    source_run_id: str  # a run ID string, or the sentinel "latest"
    # Directory the source run's artifacts live in. Defaults to the sibling
    # of the rollup run's own directory (assumes shared base_path). Set
    # explicitly when the source and target pipelines write to different
    # per-level bases (e.g. GBA's ``artifacts/gba_zone`` vs ``artifacts/gba_corp``).
    source_artifacts_dir: str | None = None

    @property
    def is_latest(self) -> bool:
        return self.source_run_id == "latest"

    @classmethod
    def from_raw(cls, raw: Mapping[str, Any]) -> RollupRunConfig:
        source_run_id = str(raw.get("source_run_id", "")).strip()
        if not source_run_id:
            raise ValueError("run.source_run_id is required in rollup config")
        source_artifacts_dir_raw = raw.get("source_artifacts_dir")
        source_artifacts_dir = (
            str(source_artifacts_dir_raw).strip() if source_artifacts_dir_raw else None
        )
        return cls(
            source_run_id=source_run_id,
            source_artifacts_dir=source_artifacts_dir,
        )


@dataclass(frozen=True)
class RollupConfig:
    source_level: str  # child level in the geojson hierarchy (e.g. "zone")
    target_level: str  # parent level (e.g. "corp")
    cases_csv: str  # parent-level cases_daily.csv for threshold recomputation
    geojson_base_path: str
    on_missing_parents: str = "error"  # "error" | "warn"

    @property
    def source_level_plural(self) -> str:
        return self.source_level + "s"

    @property
    def target_level_plural(self) -> str:
        return self.target_level + "s"

    @classmethod
    def from_raw(cls, raw: Mapping[str, Any]) -> RollupConfig:
        source_level = str(raw.get("source_level", "")).strip()
        target_level = str(raw.get("target_level", "")).strip()
        if not source_level:
            raise ValueError("rollup.source_level is required")
        if not target_level:
            raise ValueError("rollup.target_level is required")
        if source_level == target_level:
            raise ValueError(
                f"rollup.source_level and target_level must differ, got "
                f"{source_level!r} for both"
            )
        return cls(
            source_level=source_level,
            target_level=target_level,
            cases_csv=str(
                raw.get("cases_csv", "prepared_data/corp/cases_daily.csv")
            ).strip(),
            geojson_base_path=str(
                raw.get("geojson_base_path", "gba_datasets/geojsons/geojsons_GBA")
            ).strip(),
            on_missing_parents=_validated_on_missing(
                raw.get("on_missing_parents", "error")
            ),
        )


def _validated_on_missing(value: Any) -> str:
    v = str(value).strip().lower() or "error"
    if v not in ("error", "warn"):
        raise ValueError(
            f"rollup.on_missing_parents must be 'error' or 'warn', got {value!r}"
        )
    return v
