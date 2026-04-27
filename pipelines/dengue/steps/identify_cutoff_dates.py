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

        # Cross-data invariant: sampled case dates and sampled weather dates
        # MUST agree, otherwise the outer merge in train_and_predict produces
        # rows that all fail _filter_features.dropna() and NBR sees zero
        # training samples (MinMaxScaler then crashes with "0 sample(s)").
        case_dates = set(case_df["recordDate"].unique())
        weather_dates = set(weather_df["recordDate"].unique())
        intersection = case_dates & weather_dates
        smaller = min(len(case_dates), len(weather_dates)) or 1
        overlap_pct = 100.0 * len(intersection) / smaller
        if overlap_pct < 95.0:
            context.log.warning(
                "sampled date overlap is %.1f%% (case=%d weather=%d intersection=%d) "
                "— train_and_predict will likely produce empty training data; "
                "check load_prepared_{case,weather}_data for sparse/gappy inputs",
                overlap_pct,
                len(case_dates),
                len(weather_dates),
                len(intersection),
            )
        else:
            context.log.info(
                "sampled date overlap=%.1f%% (case=%d weather=%d intersection=%d)",
                overlap_pct,
                len(case_dates),
                len(weather_dates),
                len(intersection),
            )

        cutoff_case = cutoffs.estimate_cutoff_date(
            case_df, min_regions=cfg.case_min_regions
        )
        cutoff_weather = cutoffs.estimate_cutoff_date(
            weather_df, min_regions=cfg.weather_min_regions
        )

        cutoff_ts, pred_upto, prediction_dates = cutoffs.identify_cutoff_dates(
            cutoff_case, cutoff_weather
        )

        if not prediction_dates:
            raise ValueError(
                f"No prediction dates available. "
                f"Data cutoff is {cutoff_ts.date()} but run_date is "
                f"{inputs.identify_sampling_day.run_date} — the cutoff is on or after "
                f"the run date, so there are no future weeks to predict. "
                f"Either set run_date to a date well before {cutoff_ts.date()}, "
                f"or run dengue_prep to pull in more recent data."
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
