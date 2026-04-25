from __future__ import annotations

import io
from dataclasses import dataclass
from typing import ClassVar

import pandas as pd

from acestor import BaseStep, PipelineContext
from pipelines.dengue.configs import TrainPredictConfig, _section
from pipelines.dengue.lib import predictions as pred_lib
from pipelines.dengue.lib import zones
from pipelines.dengue.lib.models import nbr, tse
from pipelines.dengue.results import (
    CutoffDatesResult,
    PredictionResult,
    ThresholdsResult,
)


@dataclass(frozen=True)
class TrainAndPredictInputs:
    identify_cutoff_dates: CutoffDatesResult
    generate_thresholds: ThresholdsResult


class TrainAndPredictStep(BaseStep[TrainAndPredictInputs, PredictionResult]):
    input_type: ClassVar[type] = TrainAndPredictInputs

    def run(
        self, context: PipelineContext, inputs: TrainAndPredictInputs
    ) -> PredictionResult:
        cfg = TrainPredictConfig.from_raw(_section(context.config, "model"))

        case_path = context.artifact_path(
            f"datasets/cases_{cfg.spatial_res}_sampled.csv"
        )
        case_csv = context.artifacts.read_text(case_path)
        case_df = pd.read_csv(io.StringIO(case_csv))

        weather_path = context.artifact_path(
            f"datasets/weather_{cfg.spatial_res}_sampled.csv"
        )
        weather_csv = context.artifacts.read_text(weather_path)
        weather_df = pd.read_csv(io.StringIO(weather_csv))

        for df in [case_df, weather_df]:
            for cand in ["metadata.primaryDate", "date"]:
                if cand in df.columns:
                    df.rename(columns={cand: "recordDate"}, inplace=True)
                    break
            df["recordDate"] = pd.to_datetime(df["recordDate"])
            df["recordYear"] = df["recordDate"].dt.year
            df["ISOWeek"] = df["recordDate"].dt.isocalendar().week.astype(int)
            # Normalise spatial column to cfg.spatial_res.
            # Any location.adminX.ID or region_id column is renamed to cfg.spatial_res
            # so downstream code can always join on cfg.spatial_res regardless of data format.
            for cand in [
                "location.admin1.ID",
                "location.admin2.ID",
                "location.admin3.ID",
                "location.admin4.ID",
                "location.admin5.ID",
                "region_id",
            ]:
                if cand in df.columns and cand != cfg.spatial_res:
                    df.rename(columns={cand: cfg.spatial_res}, inplace=True)
                    break

        merged = case_df.merge(
            weather_df,
            on=[cfg.spatial_res, "recordDate", "recordYear", "ISOWeek"],
            how="outer",
        )
        merged = merged.sort_values([cfg.spatial_res, "recordDate"]).reset_index(
            drop=True
        )
        merged["recordMonth"] = merged["recordDate"].dt.month

        pred_upto = pd.Timestamp(inputs.identify_cutoff_dates.pred_upto)
        cutoff_case = pd.Timestamp(inputs.identify_cutoff_dates.cutoff_case)

        # Filter merged to valid sampling-day dates (matching SOT get_days + filter)
        sampling_day = inputs.identify_cutoff_dates.sampling_day
        valid_dates = pd.date_range(
            start=merged["recordDate"].min(),
            end=pred_upto,
            freq=sampling_day,
        )
        merged = merged[merged["recordDate"].isin(valid_dates)].reset_index(drop=True)

        nbr_pred = nbr.negative_binomial_regression(
            merged,
            spatial_col=cfg.spatial_res,
            feature_cols=cfg.data_features,
            lag_temp=cfg.lag_temp,
            lag_rf=cfg.lag_rf,
            years_to_exclude=cfg.years_to_exclude,
            years_to_include=cfg.years_to_include,
            predict_upto_date=pred_upto,
        )
        thresholds_csv = context.artifacts.read_text(
            inputs.generate_thresholds.thresholds_csv_path
        )
        precomputed_thresholds = pd.read_csv(io.StringIO(thresholds_csv))

        prediction_dfs = []
        if not nbr_pred.empty:
            nbr_out = zones.merge_predictions_thresholds(
                case_df,
                nbr_pred,
                spatial_col=cfg.spatial_res,
                list_alpha=cfg.list_alpha,
                to_date=pred_upto - pd.Timedelta(days=28),
                precomputed_thresholds=precomputed_thresholds,
            )
            prediction_dfs.append(nbr_out)
        else:
            context.log.warning(
                "train_and_predict: NBR returned no predictions — "
                "check that weather region_ids match case region_ids in prepared_data"
            )

        # TSE: always attempt for whatever spatial_res is configured
        tse_upto = cutoff_case + pd.Timedelta(days=14)
        tse_pred = tse.linear_extrapolation(
            case_df,
            spatial_col=cfg.spatial_res,
            years_to_exclude=cfg.years_to_exclude,
            years_to_include=cfg.years_to_include,
            predict_upto_date=tse_upto,
        )
        if not tse_pred.empty:
            tse_out = zones.merge_predictions_thresholds(
                case_df,
                tse_pred,
                spatial_col=cfg.spatial_res,
                list_alpha=cfg.list_alpha,
                to_date=tse_upto - pd.Timedelta(days=14),
                precomputed_thresholds=precomputed_thresholds,
            )
            prediction_dfs.append(tse_out)

        if not prediction_dfs:
            context.log.warning(
                "train_and_predict: no predictions from any model — "
                "returning empty result (all regions will appear white/hatched on maps)"
            )
            return PredictionResult(
                predictions_csv_path="",
                region_type=cfg.spatial_res,
                month_string="",
            )

        ensembled = pred_lib.ensemble_predictions(
            prediction_dfs, spatial_col=cfg.spatial_res
        )
        # "ensembleModel" when NBR + TSE combined; "negativeBinomialRegression" when only NBR
        if len(prediction_dfs) == 1:
            ensembled["model"] = "negativeBinomialRegression"

        classified = zones.classify_into_zones(ensembled, spatial_col=cfg.spatial_res)
        classified["predictionZone"] = classified["predictionZone"].fillna(0)

        zone_zero_mask = classified["predictionZone"] == 0
        if zone_zero_mask.any():
            zone_zero_regions = sorted(
                classified.loc[zone_zero_mask, cfg.spatial_res].unique().tolist()
            )
            context.log.warning(
                "train_and_predict: %d region(s) have predictionZone=0 after zone "
                "assignment (prediction fell outside all threshold pairs — will appear "
                "white/no-hatch on maps): %s",
                len(zone_zero_regions),
                zone_zero_regions,
            )

        if "Mean" in classified.columns and "StdDev" in classified.columns:
            degenerate = (classified["Mean"] == 0) & (classified["StdDev"] == 0)
            classified.loc[degenerate, "predictionZone"] = pd.NA
            if degenerate.any():
                deg_regions = sorted(
                    classified.loc[degenerate, cfg.spatial_res].unique().tolist()
                )
                context.log.warning(
                    "train_and_predict: %d region(s) have degenerate thresholds "
                    "(Mean=0, StdDev=0 — historically zero reported cases) → "
                    "predictionZone=NA, will appear light gray on maps: %s",
                    len(deg_regions),
                    deg_regions,
                )

        run_date = pd.Timestamp(inputs.identify_cutoff_dates.run_date).normalize()
        future_dates = [
            d for d in classified["startDatePredictedWeek"].unique() if d >= run_date
        ]
        month_string = (
            pred_lib.get_month_year_range(list(future_dates)) if future_dates else ""
        )
        end_str = run_date.date().strftime("%Y%m%d")

        dest = context.artifact_path(
            f"results/Predictions_{month_string}_{cfg.spatial_res.capitalize()}_{end_str}.csv"
        )
        context.artifacts.write_text(classified.to_csv(index=False), dest)
        context.log.info(
            "train_and_predict: %d predictions for %s", len(classified), cfg.spatial_res
        )

        return PredictionResult(
            predictions_csv_path=dest,
            region_type=cfg.spatial_res,
            month_string=month_string,
        )
