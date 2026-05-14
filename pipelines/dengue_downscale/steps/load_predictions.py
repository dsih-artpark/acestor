"""Discover and validate the source run's combined predictions CSV."""

from __future__ import annotations

from typing import ClassVar

import pandas as pd

from acestor import BaseStep, FileStorage, NoInputs, PipelineContext
from pipelines.dengue_downscale.configs import DownscaleRunConfig
from pipelines.dengue_downscale.results import LoadPredictionsResult

_MODEL_SUFFIXES = ("_nbr_", "_rf_", "_xgb_", "_tse_")


class LoadPredictionsStep(BaseStep[NoInputs, LoadPredictionsResult]):
    input_type: ClassVar[type] = NoInputs

    def run(self, context: PipelineContext, inputs: NoInputs) -> LoadPredictionsResult:
        cfg = DownscaleRunConfig.from_raw(context.config.get("run") or {})

        storage = context.artifacts
        if not isinstance(storage, FileStorage):
            raise TypeError(
                "LoadPredictionsStep requires a filesystem artifacts storage."
            )

        results_dir = storage.base_path / cfg.source_run_id / "results"
        if not results_dir.exists():
            raise FileNotFoundError(
                f"Source run artifacts not found at {results_dir}. "
                f"Run the dengue pipeline with run_id={cfg.source_run_id!r} first."
            )

        all_csvs = sorted(results_dir.glob("Predictions_*.csv"))
        combined = [
            p for p in all_csvs if not any(s in p.name for s in _MODEL_SUFFIXES)
        ]
        if not combined:
            raise FileNotFoundError(
                f"No combined Predictions_*.csv found in {results_dir}. "
                f"Expected a file without model-name suffix (e.g. Predictions_Mar 2026_20260301.csv)."
            )

        if len(combined) > 1:
            context.log.warning(
                "load_predictions: found %d combined Predictions CSVs in %s, using %s",
                len(combined),
                results_dir,
                combined[0].name,
            )
        pred_path = combined[0]

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
            cfg.source_run_id,
        )
        return LoadPredictionsResult(
            predictions_csv_path=str(pred_path),
            source_run_id=cfg.source_run_id,
            run_date=run_date,
        )
