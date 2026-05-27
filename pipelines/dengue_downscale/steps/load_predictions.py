"""Discover and validate the source run's combined predictions CSV."""

from __future__ import annotations

from pathlib import Path
from typing import ClassVar

import pandas as pd

from acestor import BaseStep, FileStorage, NoInputs, PipelineContext
from pipelines.dengue_downscale.configs import DownscaleRunConfig
from pipelines.dengue_downscale.results import LoadPredictionsResult

_MODEL_SUFFIXES = ("_nbr_", "_rf_", "_xgb_", "_tse_")


def _resolve_latest_run(base_path: Path) -> str:
    """Return the name of the most recently modified run dir that has a canonical predictions.csv."""
    candidates = []
    for run_dir in base_path.iterdir():
        if not run_dir.is_dir():
            continue
        results_dir = run_dir / "outputs"
        if not results_dir.exists():
            continue
        if (results_dir / "predictions.csv").exists():
            candidates.append((run_dir.stat().st_mtime, run_dir.name))
    if not candidates:
        raise FileNotFoundError(
            f"source_run_id='latest' but no valid dengue run found in {base_path}. "
            f"Run the dengue pipeline first."
        )
    candidates.sort(reverse=True)
    return candidates[0][1]


class LoadPredictionsStep(BaseStep[NoInputs, LoadPredictionsResult]):
    input_type: ClassVar[type] = NoInputs

    def run(self, context: PipelineContext, inputs: NoInputs) -> LoadPredictionsResult:
        cfg = DownscaleRunConfig.from_raw(context.config.get("run") or {})

        storage = context.artifacts
        if not isinstance(storage, FileStorage):
            raise TypeError(
                "LoadPredictionsStep requires a filesystem artifacts storage."
            )

        source_run_id = cfg.source_run_id
        if cfg.is_latest:
            source_run_id = _resolve_latest_run(storage.base_path)
            context.log.info(
                "load_predictions: source_run_id='latest' resolved to %r", source_run_id
            )

        results_dir = storage.base_path / source_run_id / "outputs"
        if not results_dir.exists():
            raise FileNotFoundError(
                f"Source run artifacts not found at {results_dir}. "
                f"Run the dengue pipeline with run_id={source_run_id!r} first."
            )

        pred_path = results_dir / "predictions.csv"
        if not pred_path.exists():
            raise FileNotFoundError(
                f"No predictions.csv found in {results_dir}. "
                f"Run the dengue pipeline with run_id={source_run_id!r} first."
            )

        try:
            df = pd.read_csv(pred_path, usecols=["dateOfComputingPrediction"])
        except ValueError as exc:
            raise ValueError(
                f"Could not read 'dateOfComputingPrediction' from {pred_path} "
                f"(source_run_id={cfg.source_run_id!r}). Is this a valid dengue predictions CSV?"
            ) from exc
        run_date = str(df["dateOfComputingPrediction"].iloc[0])

        context.log.info(
            "load_predictions: found %s (run_date=%s, source_run_id=%s)",
            pred_path.name,
            run_date,
            source_run_id,
        )
        return LoadPredictionsResult(
            predictions_csv_path=str(pred_path),
            source_run_id=source_run_id,
            run_date=run_date,
        )
