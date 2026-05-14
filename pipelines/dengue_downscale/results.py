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
