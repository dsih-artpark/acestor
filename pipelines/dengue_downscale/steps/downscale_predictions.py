"""Build mapping, compute shares, disaggregate, write output."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar

import pandas as pd

from acestor import BaseStep, PipelineContext
from pipelines.dengue_downscale.configs import DownscaleConfig
from pipelines.dengue_downscale.lib.downscale import (
    build_parent_child_mapping,
    downscale_predictions,
)
from pipelines.dengue_downscale.results import DownscaleResult, LoadPredictionsResult


@dataclass(frozen=True)
class DownscalePredictionsInputs:
    load_predictions: LoadPredictionsResult


class DownscalePredictionsStep(BaseStep[DownscalePredictionsInputs, DownscaleResult]):
    input_type: ClassVar[type] = DownscalePredictionsInputs

    def run(
        self, context: PipelineContext, inputs: DownscalePredictionsInputs
    ) -> DownscaleResult:
        cfg = DownscaleConfig.from_raw(context.config.get("downscale") or {})

        parent_preds = pd.read_csv(inputs.load_predictions.predictions_csv_path)

        cases_path = Path(cfg.cases_csv)
        if not cases_path.exists():
            raise FileNotFoundError(
                f"Child-level cases CSV not found: {cases_path}. "
                f"Run dengue_prep at the child spatial level ({cfg.child_level!r}) first."
            )
        cases_df = pd.read_csv(cases_path, parse_dates=["date"])
        as_of_date = cases_df["date"].max()
        context.log.info(
            "downscale_predictions: using as_of_date=%s (last date in cases CSV)",
            as_of_date.date(),
        )

        geojson_dir = Path(cfg.geojson_base_path) / cfg.child_level_plural
        if not geojson_dir.exists():
            raise FileNotFoundError(
                f"Geojson directory not found: {geojson_dir}. "
                f"Check downscale.geojson_base_path and downscale.child_level in config."
            )
        child_mapping = build_parent_child_mapping(geojson_dir)
        if not child_mapping:
            raise ValueError(
                f"No parent→child mapping found in {geojson_dir}. "
                f"Verify geojsons have 'region_id' and 'parent' properties."
            )

        context.log.info(
            "downscale_predictions: %d parent rows, %d children mapped, window=%d weeks",
            len(parent_preds),
            len(child_mapping),
            cfg.window_weeks,
        )

        child_preds = downscale_predictions(
            parent_preds, child_mapping, cases_df, as_of_date, cfg.window_weeks
        )

        run_date_str = inputs.load_predictions.run_date.replace("-", "")
        dest = context.artifact_path(
            f"results/Predictions_downscaled_{cfg.parent_level}_to_{cfg.child_level}_{run_date_str}.csv"
        )
        context.artifacts.write_text(child_preds.to_csv(index=False), dest)

        context.log.info(
            "downscale_predictions: %d parent rows → %d child rows, written to %s",
            len(parent_preds),
            len(child_preds),
            dest,
        )
        return DownscaleResult(
            output_csv_path=dest,
            n_parent_rows=len(parent_preds),
            n_child_rows=len(child_preds),
        )
