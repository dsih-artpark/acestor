from __future__ import annotations

import io
from dataclasses import dataclass
from typing import Any, ClassVar

import pandas as pd

from acestor import BaseStep, PipelineContext
from pipelines.dengue.configs import (
    ReportConfig,
    ThresholdsConfig,
    TrainPredictConfig,
    _section,
    resolve_model_config,
)
from pipelines.dengue.lib import predictions as pred_lib
from pipelines.dengue.lib import zones
from pipelines.dengue.lib.thresholds import icmr_quartile_zones
from pipelines.dengue.lib.ensembles import get_ensemble
from pipelines.dengue.lib.models import get_model, ModelContext
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
        thresh_cfg = ThresholdsConfig.from_raw(_section(context.config, "thresholds"))
        raw_model_configs: dict[str, Any] = dict(
            context.config.get("model_configs") or {}
        )
        unknown = set(raw_model_configs) - set(cfg.models)
        if unknown:
            raise ValueError(
                f"model_configs contains keys not in model.models: {sorted(unknown)}. "
                f"model.models = {cfg.models}"
            )
        report_cfg = ReportConfig.from_raw(
            _section(context.config, "report"),
            pipeline=_section(context.config, "pipeline"),
        )

        case_path = context.artifact_path(f"inputs/cases_{cfg.spatial_res}_sampled.csv")
        case_csv = context.artifacts.read_text(case_path)
        case_df = pd.read_csv(io.StringIO(case_csv))

        weather_path = context.artifact_path(
            f"inputs/weather_{cfg.spatial_res}_sampled.csv"
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

        thresholds_csv = context.artifacts.read_text(
            inputs.generate_thresholds.thresholds_csv_path
        )
        precomputed_thresholds = pd.read_csv(io.StringIO(thresholds_csv))

        per_model_dfs: dict[str, pd.DataFrame] = {}
        prediction_dfs: list[pd.DataFrame] = []
        for model_name in cfg.models:
            model_cfg = resolve_model_config(cfg, raw_model_configs.get(model_name, {}))
            ctx = ModelContext(
                merged_df=merged,
                case_df=case_df,
                cfg=model_cfg,
                pred_upto=pred_upto,
                cutoff_case=cutoff_case,
                artifacts=context.artifacts,
                run_id=context.run_id,
                log=context.log,
            )
            model = get_model(model_name)
            pred = model.predict(ctx)
            if pred.empty:
                context.log.error(
                    "train_and_predict: %s returned no predictions. "
                    "Most likely cause: prediction dates (%s … %s) fall after the case data "
                    "cutoff (%s), so case-lag features are NaN for all regions. "
                    "Fix: set run_date to a date within the case data window.",
                    model_name,
                    pred_upto - pd.Timedelta(weeks=3),
                    pred_upto,
                    cutoff_case.date(),
                )
                continue
            out = zones.merge_predictions_thresholds(
                case_df,
                pred,
                spatial_col=cfg.spatial_res,
                list_alpha=thresh_cfg.list_alpha,
                to_date=model.threshold_to_date(ctx),
                precomputed_thresholds=precomputed_thresholds,
            )
            per_model_dfs[model_name] = out
            prediction_dfs.append(out)

        failed_models = [m for m in cfg.models if m not in per_model_dfs]
        if failed_models:
            context.log.error(
                "train_and_predict: %d/%d configured model(s) produced no predictions: %s",
                len(failed_models),
                len(cfg.models),
                failed_models,
            )

        if not prediction_dfs:
            raise RuntimeError(
                f"train_and_predict: all configured models ({cfg.models}) returned empty "
                f"predictions — cannot continue. Check ERROR logs above for the likely cause."
            )

        # Combine via the configured ensemble strategy (or skip if "none").
        ensembled: pd.DataFrame | None
        if cfg.ensemble == "none":
            ensembled = None
        else:
            ensembled = get_ensemble(cfg.ensemble).combine(
                prediction_dfs, spatial_col=cfg.spatial_res
            )
            if len(prediction_dfs) == 1 and "model" in prediction_dfs[0].columns:
                ensembled["model"] = prediction_dfs[0]["model"].iloc[0]

        run_date = pd.Timestamp(inputs.identify_cutoff_dates.run_date).normalize()

        def _classify_and_write(df: pd.DataFrame, suffix: str) -> tuple[str, str]:
            """Classify, write, return (csv_path, month_string)."""
            classified = zones.classify_into_zones(df, spatial_col=cfg.spatial_res)
            # WHO zones are already in predictionZone from classify_into_zones — preserve as whoZone
            classified["whoZone"] = classified["predictionZone"]

            # Degenerate WHO check: Mean=0 & StdDev=0 → meaningless threshold → NaN
            if "Mean" in classified.columns and "StdDev" in classified.columns:
                degenerate = (classified["Mean"] == 0) & (classified["StdDev"] == 0)
                classified.loc[degenerate, "whoZone"] = pd.NA
                if degenerate.any():
                    deg_regions = sorted(
                        classified.loc[degenerate, cfg.spatial_res].unique().tolist()
                    )
                    context.log.warning(
                        "train_and_predict[%s]: %d region(s) have degenerate thresholds "
                        "(Mean=0, StdDev=0): %s",
                        suffix or "ensemble",
                        len(deg_regions),
                        deg_regions,
                    )

            icmr_classified = icmr_quartile_zones(classified)
            classified["icmrZone"] = icmr_classified["predictionZone"]

            if thresh_cfg.classification_method == "icmr":
                classified["predictionZone"] = classified["icmrZone"]
            else:
                classified["predictionZone"] = classified["whoZone"]
            classified["predictionZone"] = classified["predictionZone"].fillna(0)

            zone_zero_mask = classified["predictionZone"] == 0
            if zone_zero_mask.any():
                zone_zero_regions = sorted(
                    classified.loc[zone_zero_mask, cfg.spatial_res].unique().tolist()
                )
                context.log.warning(
                    "train_and_predict[%s]: %d region(s) have predictionZone=0 "
                    "(prediction fell outside all threshold pairs): %s",
                    suffix or "ensemble",
                    len(zone_zero_regions),
                    zone_zero_regions,
                )

            future_dates = [
                d
                for d in classified["startDatePredictedWeek"].unique()
                if d >= run_date
            ]
            local_month_string = (
                pred_lib.get_month_year_range(list(future_dates))
                if future_dates
                else ""
            )
            end_str = run_date.date().strftime("%Y%m%d")

            suffix_part = f"_{suffix}" if suffix else ""
            dest = context.artifact_path(
                f"outputs/Predictions_{local_month_string}_{cfg.spatial_res.capitalize()}{suffix_part}_{end_str}.csv"
            )
            context.artifacts.write_text(classified.to_csv(index=False), dest)
            context.log.info(
                "train_and_predict[%s]: %d predictions for %s",
                suffix or "ensemble",
                len(classified),
                cfg.spatial_res,
            )
            return dest, local_month_string

        written_paths: dict[str, str] = {}
        month_string = ""

        if cfg.output in ("per_model", "both"):
            for model_name, df in per_model_dfs.items():
                path, ms = _classify_and_write(df, suffix=model_name)
                written_paths[model_name] = path
                month_string = month_string or ms

        if cfg.output in ("ensemble", "both"):
            assert ensembled is not None  # guaranteed by config validation
            path, ms = _classify_and_write(ensembled, suffix="")
            written_paths["ensemble"] = path
            month_string = month_string or ms

        # Pick the CSV that downstream maps + report consume.
        if report_cfg.primary not in written_paths:
            raise ValueError(
                f"report.primary={report_cfg.primary!r} but no matching CSV was written. "
                f"Available: {sorted(written_paths)}. Check report.primary against "
                f"model.models and model.output."
            )

        return PredictionResult(
            predictions_csv_path=written_paths[report_cfg.primary],
            region_type=cfg.spatial_res,
            month_string=month_string,
        )
