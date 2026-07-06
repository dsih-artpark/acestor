"""Sum child predictions per (parent, week, method, model); re-derive zones."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar

import pandas as pd

from acestor import BaseStep, PipelineContext
from pipelines.dengue.configs import (
    ThresholdsConfig,
    _section,
    resolve_threshold_config,
)
from pipelines.dengue.lib.lgd import add_lgd_column, require_state
from pipelines.dengue.lib.thresholds import ThresholdContext
from pipelines.dengue_downscale.lib.downscale import assign_child_zones
from pipelines.dengue_rollup.configs import RollupConfig
from pipelines.dengue_rollup.lib.rollup import (
    build_child_parent_mapping,
    rollup_predictions,
)
from pipelines.dengue_rollup.results import (
    LoadRollupPredictionsResult,
    RollupResult,
)


def _build_threshold_contexts(
    raw_thresholds: dict,
) -> tuple[list[float], str, dict[str, ThresholdContext], list[float]]:
    """Parse the rollup config's `thresholds:` block into the shape the
    zone-assignment code expects."""
    cfg = ThresholdsConfig.from_raw(raw_thresholds)
    raw_method_configs = dict(raw_thresholds.get("method_configs") or {})
    ctx_by_method: dict[str, ThresholdContext] = {}
    for method in cfg.methods:
        mc = resolve_threshold_config(cfg, raw_method_configs.get(method, {}))
        ctx_by_method[method] = ThresholdContext(
            n_weeks=mc.n_weeks,
            historical_n_years=mc.historical_n_years,
            excluded_years=mc.excluded_years,
            included_years=mc.included_years,
            recent_weeks=mc.recent_weeks,
            sd_window_weeks=mc.sd_window_weeks,
            weight_recent=mc.weight_recent,
            weight_seasonal=mc.weight_seasonal,
        )
    return (
        cfg.list_alpha,
        cfg.classification_method,
        ctx_by_method,
        cfg.percentile_cutoffs,
    )


@dataclass(frozen=True)
class RollupPredictionsInputs:
    load_predictions: LoadRollupPredictionsResult


class RollupPredictionsStep(BaseStep[RollupPredictionsInputs, RollupResult]):
    input_type: ClassVar[type] = RollupPredictionsInputs

    def run(
        self, context: PipelineContext, inputs: RollupPredictionsInputs
    ) -> RollupResult:
        cfg = RollupConfig.from_raw(context.config.get("rollup") or {})

        source_preds = pd.read_csv(inputs.load_predictions.predictions_csv_path)
        # CSV-boundary reverse rename: post-#89 CSVs expose `prediction` as the
        # display int; the rollup sum operates on the raw float. Swap if the
        # source is post-#89, pass through if pre-#89.
        if "predictionRaw" in source_preds.columns:
            source_preds = source_preds.rename(
                columns={
                    "prediction": "predictionInt",
                    "predictionRaw": "prediction",
                }
            )

        # Case data for parent-level zone re-derivation.
        cases_path = Path(cfg.cases_csv)
        if not cases_path.exists():
            raise FileNotFoundError(
                f"Parent-level cases CSV not found: {cases_path}. "
                f"Run dengue_prep at target_level={cfg.target_level!r} first."
            )
        cases_df = pd.read_csv(cases_path, parse_dates=["date"])
        as_of_date = cases_df["date"].max()
        context.log.info(
            "rollup_predictions: using as_of_date=%s (last date in cases CSV)",
            as_of_date.date(),
        )

        # Child-parent mapping — geojsons live at the *source* (child) level.
        geojson_dir = Path(cfg.geojson_base_path) / cfg.source_level_plural
        if not geojson_dir.exists():
            raise FileNotFoundError(
                f"Geojson directory not found: {geojson_dir}. "
                f"Check rollup.geojson_base_path and rollup.source_level in "
                f"config."
            )
        child_mapping = build_child_parent_mapping(geojson_dir)
        if not child_mapping:
            raise ValueError(
                f"No child→parent mapping found in {geojson_dir}. "
                f"Verify geojsons have 'region_id' and 'parent' properties."
            )

        context.log.info(
            "rollup_predictions: %d source rows, %d children mapped",
            len(source_preds),
            len(child_mapping),
        )

        target_preds = rollup_predictions(
            source_preds,
            child_mapping,
            on_missing_parents=cfg.on_missing_parents,
        )

        if target_preds.empty:
            raise ValueError(
                "rollup_predictions: no target rows produced. This usually "
                "means the child mapping and source predictions disagreed on "
                "regionIDs — check that source_level matches the source CSV."
            )

        # Zone re-derivation at parent level. Rename `prediction` (raw float in
        # our rolled-up view) back to `prediction` for the classify call, which
        # expects that column name.
        thresh_section = _section(context.config, "thresholds")
        (
            list_alpha,
            classification_method,
            ctx_by_method,
            percentile_cutoffs,
        ) = _build_threshold_contexts(thresh_section)
        context.log.info(
            "rollup_predictions: zone re-derivation — classification=%s, "
            "list_alpha=%s, methods=%s",
            classification_method,
            list_alpha,
            sorted(ctx_by_method),
        )
        target_preds = target_preds.rename(
            columns={"predictionRaw": "prediction", "prediction": "predictionInt"}
        )
        # After the rename above, ``target_preds["prediction"]`` is the raw
        # float — which is what ``assign_child_zones`` reads for thresholding.
        target_preds["predictionZone"] = assign_child_zones(
            target_preds,
            cases_df,
            as_of_date,
            list_alpha=list_alpha,
            classification_method=classification_method,
            ctx_by_method=ctx_by_method,
            percentile_cutoffs=percentile_cutoffs,
        )

        # Swap the columns back to the canonical CSV boundary schema:
        # `prediction` = display int, `predictionRaw` = raw float.
        target_preds = target_preds.rename(
            columns={"prediction": "predictionRaw", "predictionInt": "prediction"}
        )

        state = require_state(context.config)
        target_preds = add_lgd_column(
            target_preds, state=state, spatial_res=cfg.target_level
        )

        dest = context.artifact_path("outputs/predictions.csv")
        context.artifacts.write_text(target_preds.to_csv(index=False), dest)
        context.log.info(
            "rollup_predictions: %d source rows → %d target rows, written to %s",
            len(source_preds),
            len(target_preds),
            dest,
        )

        # Diagnostic: which parents are covered / missing.
        source_parents_present = set(target_preds["regionID"].unique())
        all_parents = set(child_mapping.values())
        missing_parents = sorted(all_parents - source_parents_present)
        if missing_parents:
            context.log.warning(
                "rollup_predictions: %d parent(s) have no children in the source "
                "CSV: %s",
                len(missing_parents),
                missing_parents,
            )

        return RollupResult(
            output_csv_path=dest,
            n_source_rows=len(source_preds),
            n_target_rows=len(target_preds),
            n_parents_missing_children=len(missing_parents),
            missing_parent_ids=tuple(missing_parents),
        )
