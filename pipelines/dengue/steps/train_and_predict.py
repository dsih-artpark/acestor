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
from pipelines.dengue.lib.lgd import add_lgd_column, require_state
from pipelines.dengue.lib.thresholds import (
    icmr_quartile_zones,
    percentile_historical_zones,
)
from pipelines.dengue.lib.ensembles import get_ensemble
from pipelines.dengue.lib.models import get_model, ModelContext
from pipelines.dengue.results import (
    CutoffDatesResult,
    PredictionResult,
    ThresholdsResult,
)


def _resolve_predictions_paths(
    *,
    output_mode: str,
    model_names: list[str],
    primary: str,
) -> dict[str, str]:
    """Return {key: artifact_path} for predictions writes.

    Keys are usually model names; ``_canonical_primary`` is a special key
    for the per_model mode's canonical copy at the outputs/ root.

    Layout:
      - ensemble mode → {"ensembleModel": "outputs/predictions.csv"}
      - both mode → ensemble at outputs/predictions.csv, others at outputs/per_model/predictions_<m>.csv
      - per_model mode → each at outputs/per_model/predictions_<m>.csv, plus a
        canonical copy at outputs/predictions.csv keyed as "_canonical_primary"
        (the step writes ``primary``'s rows to that path).
    """
    if output_mode not in ("ensemble", "both", "per_model"):
        raise ValueError(
            f"output_mode must be ensemble|both|per_model, got {output_mode!r}"
        )

    paths: dict[str, str] = {}
    if output_mode in ("ensemble", "both"):
        paths["ensembleModel"] = "outputs/predictions.csv"
    if output_mode in ("both", "per_model"):
        for m in model_names:
            if m == "ensembleModel":
                continue  # already at root
            paths[m] = f"outputs/per_model/predictions_{m}.csv"
    if output_mode == "per_model":
        if primary not in paths:
            raise ValueError(
                f"report.primary={primary!r} not in resolved per-model paths {sorted(paths)}"
            )
        paths["_canonical_primary"] = "outputs/predictions.csv"
    return paths


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
                model_params=dict(raw_model_configs.get(model_name, {}) or {}),
                prediction_dates=list(
                    inputs.identify_cutoff_dates.prediction_dates or []
                ),
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
            # Any model dropout — partial or total — fails the run. Previously
            # partial dropouts were log.error-only and the run continued with
            # status="success", indistinguishable from a healthy run to any
            # caller checking the exit code (issue #65). The all-empty case
            # already raised; this raises on the partial case too so the run
            # status reflects the degraded outcome.
            raise RuntimeError(
                f"train_and_predict: {len(failed_models)}/{len(cfg.models)} "
                f"configured model(s) returned empty predictions: "
                f"{failed_models}. Likely cause: prediction dates fall past the "
                f"case-data cutoff, NaN-ing case-lag features. Fix: set "
                f"run_date to a date within the case data window, or re-run "
                f"dengue_prep to refresh prepared_data/."
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

        def _classify_and_write(
            df: pd.DataFrame, *, suffix: str, dest_key: str
        ) -> tuple[pd.DataFrame, str]:
            """Classify into zones and write the classified CSV to dest_key.
            Returns (classified_df, month_string).
            """
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

            percentile_classified = percentile_historical_zones(
                classified,
                case_df,
                spatial_col=cfg.spatial_res,
                percentile_cutoffs=thresh_cfg.percentile_cutoffs,
            )
            classified["percentileZone"] = percentile_classified["predictionZone"]

            if thresh_cfg.classification_method == "icmr":
                classified["predictionZone"] = classified["icmrZone"]
            elif thresh_cfg.classification_method == "percentile":
                classified["predictionZone"] = classified["percentileZone"]
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

            if cfg.spatial_res in classified.columns and cfg.spatial_res != "regionID":
                classified = classified.drop(columns=[cfg.spatial_res])
            state = require_state(context.config)
            classified = add_lgd_column(
                classified, state=state, spatial_res=cfg.spatial_res
            )
            dest = context.artifact_path(dest_key)
            context.artifacts.write_text(classified.to_csv(index=False), dest)
            context.log.info(
                "train_and_predict[%s]: %d predictions for %s → %s",
                suffix or "ensemble",
                len(classified),
                cfg.spatial_res,
                dest_key,
            )
            return classified, local_month_string

        # Map cfg.primary ("ensemble" in configs) → internal model name ("ensembleModel").
        primary_model = (
            "ensembleModel" if report_cfg.primary == "ensemble" else report_cfg.primary
        )
        model_names_for_resolver = list(per_model_dfs.keys())
        if cfg.output in ("ensemble", "both"):
            model_names_for_resolver = model_names_for_resolver + ["ensembleModel"]

        paths_map = _resolve_predictions_paths(
            output_mode=cfg.output,
            model_names=model_names_for_resolver,
            primary=primary_model,
        )

        written: dict[str, str] = {}
        month_string = ""

        if cfg.output in ("per_model", "both"):
            for model_name, df in per_model_dfs.items():
                dest = paths_map[model_name]
                _, ms = _classify_and_write(df, suffix=model_name, dest_key=dest)
                written[model_name] = dest
                month_string = month_string or ms

        if cfg.output in ("ensemble", "both"):
            assert ensembled is not None  # guaranteed by config validation
            dest = paths_map["ensembleModel"]
            _, ms = _classify_and_write(ensembled, suffix="", dest_key=dest)
            written["ensembleModel"] = dest
            month_string = month_string or ms

        if cfg.output == "per_model":
            # Copy primary model's CSV to outputs/predictions.csv so there is always
            # one canonical file at the root for officials / the HTML brief.
            primary_text = context.artifacts.read_text(
                context.artifact_path(written[primary_model])
            )
            canonical_dest = context.artifact_path(paths_map["_canonical_primary"])
            context.artifacts.write_text(primary_text, canonical_dest)
            written["_canonical_primary"] = paths_map["_canonical_primary"]

        # The canonical CSV is always outputs/predictions.csv in every mode.
        canonical = (
            paths_map.get("_canonical_primary")
            or paths_map.get("ensembleModel")
            or written[primary_model]
        )

        return PredictionResult(
            predictions_csv_path=context.artifact_path(canonical),
            region_type=cfg.spatial_res,
            month_string=month_string,
        )
