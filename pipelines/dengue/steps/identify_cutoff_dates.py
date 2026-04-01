from __future__ import annotations

import io
from dataclasses import dataclass
from typing import ClassVar

import pandas as pd

from acestor import BaseStep, PipelineContext
from pipelines.dengue.configs import CutoffConfig, _section
from pipelines.dengue.lib import cutoffs
from pipelines.dengue.results import (
    CutoffDatesResult,
    DataSufficiencyResult,
    ParseWeatherDataResult,
    SamplingDayResult,
)


@dataclass(frozen=True)
class IdentifyCutoffDatesInputs:
    validate_case_data_sufficiency: DataSufficiencyResult
    parse_weather_data: ParseWeatherDataResult
    identify_sampling_day: SamplingDayResult


class IdentifyCutoffDatesStep(BaseStep[IdentifyCutoffDatesInputs, CutoffDatesResult]):
    input_type: ClassVar[type] = IdentifyCutoffDatesInputs

    def run(
        self, context: PipelineContext, inputs: IdentifyCutoffDatesInputs
    ) -> CutoffDatesResult:
        cfg = CutoffConfig.from_raw(_section(context.config, "cutoff"))

        case_csv = context.artifacts.read_text(
            inputs.validate_case_data_sufficiency.case_result.sampled_csv_path
        )
        weather_csv = context.artifacts.read_text(
            inputs.parse_weather_data.weather_csv_path
        )

        case_df = pd.read_csv(io.StringIO(case_csv))
        weather_df = pd.read_csv(io.StringIO(weather_csv))

        date_col = (
            "metadata.primaryDate"
            if "metadata.primaryDate" in case_df.columns
            else "date"
        )
        case_df.rename(columns={date_col: "recordDate"}, inplace=True)
        case_df["recordDate"] = pd.to_datetime(case_df["recordDate"])

        w_date_col = (
            "metadata.primaryDate"
            if "metadata.primaryDate" in weather_df.columns
            else "date"
        )
        weather_df.rename(columns={w_date_col: "recordDate"}, inplace=True)
        weather_df["recordDate"] = pd.to_datetime(weather_df["recordDate"])

        cutoff_case = cutoffs.estimate_cutoff_date(
            case_df, min_regions=cfg.case_min_regions
        )
        cutoff_weather = cutoffs.estimate_cutoff_date(
            weather_df, min_regions=cfg.weather_min_regions
        )

        cutoff_ts, pred_upto, prediction_dates = cutoffs.identify_cutoff_dates(
            cutoff_case, cutoff_weather
        )

        context.write_artifact_json(
            "cutoffs.json",
            {
                "cutoff": str(cutoff_ts.date()),
                "pred_upto": str(pred_upto.date()),
                "cutoff_case": str(cutoff_case.date()),
                "cutoff_weather": str(cutoff_weather.date()),
                "prediction_dates": prediction_dates,
            },
        )
        context.log.info(
            "cutoff=%s pred_upto=%s dates=%s",
            cutoff_ts.date(),
            pred_upto.date(),
            prediction_dates,
        )

        return CutoffDatesResult(
            cutoff=str(cutoff_ts.date()),
            pred_upto=str(pred_upto.date()),
            cutoff_case=str(cutoff_case.date()),
            cutoff_weather=str(cutoff_weather.date()),
            sampling_day=inputs.identify_sampling_day.sampling_day,
            run_date=inputs.identify_sampling_day.run_date,
            prediction_dates=prediction_dates,
        )
