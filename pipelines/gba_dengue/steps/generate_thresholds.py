from __future__ import annotations

import io
from dataclasses import dataclass
from typing import ClassVar

import pandas as pd

from acestor import BaseStep, PipelineContext
from pipelines.gba_dengue.configs import ThresholdsConfig, _section
from pipelines.gba_dengue.lib import thresholds
from pipelines.gba_dengue.results import CutoffDatesResult, ThresholdsResult


@dataclass(frozen=True)
class GenerateThresholdsInputs:
    identify_cutoff_dates: CutoffDatesResult


class GenerateThresholdsStep(BaseStep[GenerateThresholdsInputs, ThresholdsResult]):
    input_type: ClassVar[type] = GenerateThresholdsInputs

    def run(
        self, context: PipelineContext, inputs: GenerateThresholdsInputs
    ) -> ThresholdsResult:
        cfg = ThresholdsConfig.from_raw(_section(context.config, "thresholds"))

        case_path = context.artifact_path(
            f"datasets/cases_{cfg.region_type}_sampled.csv"
        )
        case_csv = context.artifacts.read_text(case_path)
        df = pd.read_csv(io.StringIO(case_csv))

        if "region_id" not in df.columns:
            for candidate in [
                "location.admin4.ID",
                "location.admin3.ID",
                "location.admin2.ID",
            ]:
                if candidate in df.columns:
                    df.rename(columns={candidate: "region_id"}, inplace=True)
                    break
        if "date" not in df.columns and "metadata.primaryDate" in df.columns:
            df.rename(columns={"metadata.primaryDate": "date"}, inplace=True)

        aligned = thresholds.align_dates_all_regions(df)

        prev_n = thresholds.prev_nweeks_threshold_params(aligned, n=cfg.n_weeks)
        hist = thresholds.historical_threshold_params(
            aligned,
            n_years=cfg.historical_n_years,
            excluded_years=cfg.excluded_years,
        )
        combined = thresholds.combine_thresholds([prev_n, hist])

        dest = context.artifact_path(
            f"datasets/thresholds/{cfg.region_type}_all_thresholds.csv"
        )
        context.artifacts.write_text(combined.to_csv(index=False), dest)
        context.log.info(
            "generate_thresholds: %d rows for %s", len(combined), cfg.region_type
        )

        return ThresholdsResult(thresholds_csv_path=dest, region_type=cfg.region_type)
