from __future__ import annotations

import io
from dataclasses import dataclass
from typing import ClassVar

import pandas as pd

from acestor import BaseStep, PipelineContext
from pipelines.gba_dengue.configs import AssessConfig, _section
from pipelines.gba_dengue.lib import thresholds
from pipelines.gba_dengue.results import (
    CombinedPredictionsResult,
    ThresholdAssessmentResult,
)


@dataclass(frozen=True)
class AssessThresholdsInputs:
    combine_predictions: CombinedPredictionsResult


class AssessThresholdsStep(BaseStep[AssessThresholdsInputs, ThresholdAssessmentResult]):
    input_type: ClassVar[type] = AssessThresholdsInputs

    def run(
        self, context: PipelineContext, inputs: AssessThresholdsInputs
    ) -> ThresholdAssessmentResult:
        cfg = AssessConfig.from_raw(_section(context.config, "assess"))

        pred_csv = context.artifacts.read_text(
            inputs.combine_predictions.combined_csv_path
        )
        df = pd.read_csv(io.StringIO(pred_csv))

        _, best_corp = thresholds.assess_thresholds(
            df, region_prefix="corp", total_regions_overall=cfg.total_corp_regions
        )
        _, best_zone = thresholds.assess_thresholds(
            df, region_prefix="zone", total_regions_overall=cfg.total_zone_regions
        )

        end_str = pd.Timestamp.today().date().strftime("%Y%m%d")
        corp_dest = context.artifact_path(f"dumps/best_method_corp_{end_str}.csv")
        zone_dest = context.artifact_path(f"dumps/best_method_zone_{end_str}.csv")
        context.artifacts.write_text(best_corp.to_csv(index=False), corp_dest)
        context.artifacts.write_text(best_zone.to_csv(index=False), zone_dest)
        context.log.info(
            "assess_thresholds: corp=%d zone=%d rows", len(best_corp), len(best_zone)
        )

        return ThresholdAssessmentResult(
            best_method_corp_csv=corp_dest,
            best_method_zone_csv=zone_dest,
        )
