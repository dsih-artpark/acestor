from __future__ import annotations

import io
from dataclasses import dataclass
from typing import ClassVar

import pandas as pd

from acestor import BaseStep, PipelineContext
from pipelines.dengue.configs import MapsConfig, _section
from pipelines.dengue.lib import maps
from pipelines.dengue.sources import filesystem as geojson_sources
from pipelines.dengue.results import (
    MapsResult,
    PredictionResult,
    ThresholdAssessmentResult,
)


@dataclass(frozen=True)
class GenerateMapsInputs:
    assess_thresholds: ThresholdAssessmentResult
    train_and_predict: PredictionResult


class GenerateMapsStep(BaseStep[GenerateMapsInputs, MapsResult]):
    input_type: ClassVar[type] = GenerateMapsInputs

    def run(self, context: PipelineContext, inputs: GenerateMapsInputs) -> MapsResult:
        cfg = MapsConfig.from_raw(_section(context.config, "maps"))
        plots_dir = str(context.artifact_fs_path(cfg.output_dir))
        geojson_base = geojson_sources.get_geojson_base_dir()
        run_date = str(
            (_section(context.config, "run") or {}).get("run_date", "") or ""
        ).strip()

        pred_csv = context.artifacts.read_text(
            inputs.train_and_predict.predictions_csv_path
        )
        df = pd.read_csv(io.StringIO(pred_csv))

        known_regions = ["corp", "zone", "ward", "district", "subdistrict", "mandal"]
        present_regions = [
            r for r in known_regions if df["regionID"].str.startswith(r).any()
        ]

        generated: list[str] = []
        for region in present_regions:
            region_df = df[df["regionID"].str.startswith(region)].reset_index(drop=True)
            if region_df.empty:
                continue
            for thisdate in region_df["startDatePredictedWeek"].unique():
                date_df = region_df[region_df["startDatePredictedWeek"] == thisdate]
                for model in date_df["model"].unique():
                    model_df = date_df[date_df["model"] == model]
                    if model_df.empty:
                        continue
                    for threshold in model_df["thresholdMethod"].unique():
                        color_df = model_df[model_df["thresholdMethod"] == threshold]
                        path = maps.gen_plot(
                            color_df,
                            region=region,
                            model=model,
                            threshold=threshold,
                            thisdate=str(thisdate),
                            geojson_base=geojson_base,
                            output_dir=plots_dir,
                            figure_title=cfg.figure_title,
                            run_date=run_date,
                        )
                        if path:
                            generated.append(path)

        context.log.info("generate_maps: %d maps generated", len(generated))
        return MapsResult(map_paths=generated)
