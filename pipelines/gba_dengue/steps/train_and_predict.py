from __future__ import annotations

import io
from dataclasses import dataclass
from typing import ClassVar

import pandas as pd

from acestor import BaseStep, PipelineContext
from pipelines.gba_dengue.configs import TrainPredictConfig, _section
from pipelines.gba_dengue.lib import predictions as pred_lib
from pipelines.gba_dengue.lib import zones
from pipelines.gba_dengue.lib.models import nbr, tse
from pipelines.gba_dengue.results import (
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
            df["recordMonth"] = df["recordDate"].dt.month
            df["ISOWeek"] = df["recordDate"].dt.isocalendar().week.astype(int)
            # Normalise spatial column to cfg.spatial_res
            for cand, target in [
                ("location.admin3.ID", "zone"),
                ("location.admin2.ID", "corp"),
                ("location.admin4.ID", "ward"),
                ("region_id", cfg.spatial_res),
            ]:
                if cand in df.columns and cand != cfg.spatial_res:
                    new_name = target if target != cfg.spatial_res else cfg.spatial_res
                    df.rename(columns={cand: new_name}, inplace=True)
                    break

        merged = case_df.merge(
            weather_df,
            on=[cfg.spatial_res, "recordDate", "recordYear", "ISOWeek"],
            how="outer",
        )
        merged = merged.sort_values([cfg.spatial_res, "recordDate"]).reset_index(
            drop=True
        )

        pred_upto = pd.Timestamp(inputs.identify_cutoff_dates.pred_upto)
        cutoff_case = pd.Timestamp(inputs.identify_cutoff_dates.cutoff_case)

        nbr_pred = nbr.negative_binomial_regression(
            merged,
            spatial_col=cfg.spatial_res,
            feature_cols=cfg.data_features,
            lag_temp=cfg.lag_temp,
            lag_rf=cfg.lag_rf,
            years_to_exclude=cfg.years_to_exclude,
            predict_upto_date=pred_upto,
        )
        nbr_out = zones.merge_predictions_thresholds(
            case_df,
            nbr_pred,
            spatial_col=cfg.spatial_res,
            list_alpha=cfg.list_alpha,
            to_date=pred_upto - pd.Timedelta(days=28),
        )

        prediction_dfs = [nbr_out]

        tse_upto = cutoff_case + pd.Timedelta(days=14)
        tse_pred = tse.linear_extrapolation(
            case_df,
            spatial_col=cfg.spatial_res,
            years_to_exclude=cfg.years_to_exclude,
            predict_upto_date=tse_upto,
        )
        if not tse_pred.empty:
            tse_out = zones.merge_predictions_thresholds(
                case_df,
                tse_pred,
                spatial_col=cfg.spatial_res,
                list_alpha=cfg.list_alpha,
                to_date=tse_upto - pd.Timedelta(days=14),
            )
            prediction_dfs.append(tse_out)

        ensembled = pred_lib.ensemble_predictions(
            prediction_dfs, spatial_col=cfg.spatial_res
        )
        classified = zones.classify_into_zones(ensembled, spatial_col=cfg.spatial_res)
        classified["predictionZone"] = classified["predictionZone"].fillna(0)

        future_dates = [
            d
            for d in classified["startDatePredictedWeek"].unique()
            if d >= pd.Timestamp.today().normalize()
        ]
        month_string = (
            pred_lib.get_month_year_range(list(future_dates)) if future_dates else ""
        )
        end_str = pd.Timestamp.today().date().strftime("%Y%m%d")

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
