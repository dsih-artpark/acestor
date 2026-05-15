from __future__ import annotations

import io
from dataclasses import dataclass
from typing import ClassVar

import pandas as pd

from acestor import BaseStep, PipelineContext
from pipelines.dengue.configs import AssessConfig, _section
from pipelines.dengue.lib import report as report_lib
from pipelines.dengue.lib import thresholds
from pipelines.dengue.results import (
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

        # Detect which region types are actually present in the combined predictions.
        known_regions = ["corp", "zone", "ward", "district", "subdistrict", "mandal"]
        present_regions = [
            r for r in known_regions if df["regionID"].str.startswith(r).any()
        ]

        run_date = str(
            (_section(context.config, "run") or {}).get("run_date", "") or ""
        ).strip()
        end_str = (
            pd.Timestamp(run_date).date().strftime("%Y%m%d")
            if run_date
            else pd.Timestamp.today().date().strftime("%Y%m%d")
        )
        best_method_by_region: dict[str, str] = {}

        for region in present_regions:
            total = cfg.total_regions_by_type.get(region, 10)
            _, best = thresholds.assess_thresholds(
                df, region_prefix=region, total_regions_overall=total
            )
            # SOT: generate_LaTeX_fig_captions before writing best-method CSVs
            # (AssessThresholdPerformance.py L145–149).
            if len(best) > 0:
                best = report_lib.add_latex_figure_metadata(best, run_date=run_date)
            dest = context.artifact_path(f"outputs/best_method_{region}_{end_str}.csv")
            context.artifacts.write_text(best.to_csv(index=False), dest)
            best_method_by_region[region] = dest
            context.log.info("assess_thresholds: %s=%d rows", region, len(best))

        return ThresholdAssessmentResult(best_method_by_region=best_method_by_region)
