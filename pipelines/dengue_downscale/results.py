"""Typed result dataclasses for the dengue_downscale pipeline."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class LoadPredictionsResult:
    predictions_csv_path: str  # absolute path on disk to source predictions CSV
    source_run_id: str
    run_date: str  # dateOfComputingPrediction from source CSV


@dataclass(frozen=True)
class DownscaleResult:
    output_csv_path: str  # storage key: {run_id}/results/Predictions_downscaled_*.csv
    n_parent_rows: int
    n_child_rows: int
    # Sanity/heterogeneity diagnostics (see downscale_diagnostics)
    n_parents_uniform: int = 0  # parents split evenly (no-data uniform fallback)
    n_weeks_children_below_parent: int = 0  # child max zone < parent zone
    n_weeks_children_above_parent: int = 0  # child max zone > parent zone
    conservation_max_abs_err: float = 0.0  # max |sum(child) - parent| across weeks
    n_dropped_parents: int = 0  # parents with no children (only when on_missing_parents="warn")
    dropped_parent_ids: tuple = ()  # their regionIDs, for traceability
