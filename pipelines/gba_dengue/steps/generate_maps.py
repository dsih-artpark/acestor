from __future__ import annotations

import io
from dataclasses import dataclass
from typing import ClassVar

import pandas as pd

from acestor import BaseStep, PipelineContext
from pipelines.gba_dengue.configs import MapsConfig, _section
from pipelines.gba_dengue.lib import maps
from pipelines.gba_dengue.sources import filesystem as geojson_sources
from pipelines.gba_dengue.results import (
    CombinedPredictionsResult,
    MapsResult,
    ThresholdAssessmentResult,
)


@dataclass(frozen=True)
class GenerateMapsInputs:
    assess_thresholds: ThresholdAssessmentResult
    combine_predictions: CombinedPredictionsResult


class GenerateMapsStep(BaseStep[GenerateMapsInputs, MapsResult]):
    input_type: ClassVar[type] = GenerateMapsInputs

    def run(self, context: PipelineContext, inputs: GenerateMapsInputs) -> MapsResult:
        cfg = MapsConfig.from_raw(_section(context.config, "maps"))
        plots_dir = str(context.artifact_fs_path(cfg.output_dir))
        geojson_base = geojson_sources.get_geojson_base_dir()

        pred_csv = context.artifacts.read_text(
            inputs.combine_predictions.combined_csv_path
        )
        df = pd.read_csv(io.StringIO(pred_csv))

        generated: list[str] = []
        for region in ["corp", "zone"]:
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
                        )
                        if path:
                            generated.append(path)

        context.log.info("generate_maps: %d maps generated", len(generated))
        return MapsResult(map_paths=generated)
