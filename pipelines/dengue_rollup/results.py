"""Typed result dataclasses for the dengue_rollup pipeline."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class LoadRollupPredictionsResult:
    predictions_csv_path: str
    source_run_id: str
    run_date: str  # dateOfComputingPrediction from source CSV


@dataclass(frozen=True)
class RollupResult:
    output_csv_path: str
    n_source_rows: int
    n_target_rows: int
    n_parents_missing_children: int = 0
    missing_parent_ids: tuple = ()


@dataclass(frozen=True)
class RollupBriefResult:
    report_path: str  # outputs/report.html
    charts_dir: str  # outputs/charts/
