"""Discover the source run's predictions.csv (child-level for rollup)."""

from __future__ import annotations

from pathlib import Path
from typing import ClassVar

import pandas as pd

from acestor import BaseStep, FileStorage, NoInputs, PipelineContext
from pipelines.dengue_rollup.configs import RollupConfig, RollupRunConfig
from pipelines.dengue_rollup.results import LoadRollupPredictionsResult


def _resolve_latest_run(base_path: Path, source_level: str) -> str:
    """Return the name of the most recently modified run dir whose
    predictions.csv is at ``source_level`` granularity.

    Filters out runs whose region_id prefix doesn't match the configured
    source level — otherwise "latest" may pick a parent-level or a
    downscale-output run and either crash or produce nonsense.
    """
    source_prefix = f"{source_level}_"
    candidates: list[tuple[float, str]] = []
    for run_dir in base_path.iterdir():
        if not run_dir.is_dir():
            continue
        pred_path = run_dir / "outputs" / "predictions.csv"
        if not pred_path.exists():
            continue
        try:
            head = pd.read_csv(pred_path, usecols=["regionID"], nrows=1)
        except Exception:
            continue
        if head.empty:
            continue
        rid = str(head["regionID"].iloc[0])
        if not rid.startswith(source_prefix):
            continue
        candidates.append((run_dir.stat().st_mtime, run_dir.name))
    if not candidates:
        raise FileNotFoundError(
            f"source_run_id='latest' but no forecast run at source_level="
            f"{source_level!r} found in {base_path}. Either run the dengue "
            f"pipeline at this region_type first, or set source_run_id "
            f"explicitly in the rollup config."
        )
    candidates.sort(reverse=True)
    return candidates[0][1]


class LoadPredictionsStep(BaseStep[NoInputs, LoadRollupPredictionsResult]):
    input_type: ClassVar[type] = NoInputs

    def run(
        self, context: PipelineContext, inputs: NoInputs
    ) -> LoadRollupPredictionsResult:
        run_cfg = RollupRunConfig.from_raw(context.config.get("run") or {})
        rollup_cfg = RollupConfig.from_raw(context.config.get("rollup") or {})

        storage = context.artifacts
        if not isinstance(storage, FileStorage):
            raise TypeError(
                "LoadPredictionsStep requires a filesystem artifacts storage."
            )

        # Where the source run lives — defaults to the sibling of the current
        # run's dir (shared base_path). Override via run.source_artifacts_dir
        # when parent and child pipelines write to different per-level bases.
        source_base = (
            Path(run_cfg.source_artifacts_dir)
            if run_cfg.source_artifacts_dir
            else Path(storage.base_path).parent
        )

        source_run_id = run_cfg.source_run_id
        if run_cfg.is_latest:
            source_run_id = _resolve_latest_run(source_base, rollup_cfg.source_level)
            context.log.info(
                "load_predictions: source_run_id='latest' resolved to %r in %s",
                source_run_id,
                source_base,
            )

        source_csv = source_base / source_run_id / "outputs" / "predictions.csv"
        if not source_csv.exists():
            raise FileNotFoundError(
                f"Source predictions.csv not found: {source_csv}. "
                f"Check run.source_run_id in your rollup config."
            )

        # Read a single row just to surface the run_date for the result.
        head = pd.read_csv(source_csv, nrows=1)
        run_date = str(head["dateOfComputingPrediction"].iloc[0])
        context.log.info(
            "load_predictions: found predictions.csv (run_date=%s, source_run_id=%s)",
            run_date,
            source_run_id,
        )

        return LoadRollupPredictionsResult(
            predictions_csv_path=str(source_csv),
            source_run_id=source_run_id,
            run_date=run_date,
        )
