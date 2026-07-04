"""Build mapping, compute shares, disaggregate, write output."""

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
from pipelines.dengue_downscale.configs import DownscaleConfig
from pipelines.dengue_downscale.lib.downscale import (
    build_parent_child_mapping,
    downscale_diagnostics,
    downscale_predictions,
)
from pipelines.dengue_downscale.results import DownscaleResult, LoadPredictionsResult


def _build_threshold_contexts(
    raw_thresholds: dict,
) -> tuple[list[float], str, dict[str, ThresholdContext], list[float]]:
    """Parse the downscale config's `thresholds:` block into the same shape the
    dengue pipeline uses, so child zones are derived with matching settings.

    Returns (list_alpha, classification_method, {method: ThresholdContext},
    percentile_cutoffs).
    """
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
class DownscalePredictionsInputs:
    load_predictions: LoadPredictionsResult


class DownscalePredictionsStep(BaseStep[DownscalePredictionsInputs, DownscaleResult]):
    input_type: ClassVar[type] = DownscalePredictionsInputs

    def run(
        self, context: PipelineContext, inputs: DownscalePredictionsInputs
    ) -> DownscaleResult:
        cfg = DownscaleConfig.from_raw(context.config.get("downscale") or {})

        parent_preds = pd.read_csv(inputs.load_predictions.predictions_csv_path)

        cases_path = Path(cfg.cases_csv)
        if not cases_path.exists():
            raise FileNotFoundError(
                f"Child-level cases CSV not found: {cases_path}. "
                f"Run dengue_prep at the child spatial level ({cfg.child_level!r}) first."
            )
        cases_df = pd.read_csv(cases_path, parse_dates=["date"])
        as_of_date = cases_df["date"].max()
        context.log.info(
            "downscale_predictions: using as_of_date=%s (last date in cases CSV)",
            as_of_date.date(),
        )

        geojson_dir = Path(cfg.geojson_base_path) / cfg.child_level_plural
        if not geojson_dir.exists():
            raise FileNotFoundError(
                f"Geojson directory not found: {geojson_dir}. "
                f"Check downscale.geojson_base_path and downscale.child_level in config."
            )
        child_mapping = build_parent_child_mapping(geojson_dir)
        if not child_mapping:
            raise ValueError(
                f"No parent→child mapping found in {geojson_dir}. "
                f"Verify geojsons have 'region_id' and 'parent' properties."
            )

        context.log.info(
            "downscale_predictions: %d parent rows, %d children mapped, window=%d weeks",
            len(parent_preds),
            len(child_mapping),
            cfg.window_weeks,
        )

        (
            list_alpha,
            classification_method,
            ctx_by_method,
            percentile_cutoffs,
        ) = _build_threshold_contexts(_section(context.config, "thresholds"))
        context.log.info(
            "downscale_predictions: zone re-derivation — classification=%s, list_alpha=%s, "
            "methods=%s",
            classification_method,
            list_alpha,
            sorted(ctx_by_method),
        )

        child_preds = downscale_predictions(
            parent_preds,
            child_mapping,
            cases_df,
            as_of_date,
            cfg.window_weeks,
            list_alpha=list_alpha,
            classification_method=classification_method,
            ctx_by_method=ctx_by_method,
            percentile_cutoffs=percentile_cutoffs,
            on_missing_parents=cfg.on_missing_parents,
            historical_fallback_weeks=cfg.historical_fallback_weeks,
        )

        # Parents present in the predictions but with no children in the mapping.
        # In "error" mode downscale_predictions raises before here; in "warn" mode
        # they're dropped, so surface them on the result for traceability.
        dropped = sorted(set(parent_preds["regionID"]) - set(child_mapping.values()))
        if dropped:
            context.log.warning(
                "downscale_predictions: %d parent(s) dropped (no children mapped): %s",
                len(dropped),
                dropped,
            )

        diag = downscale_diagnostics(parent_preds, child_preds, child_mapping)
        tier_stats = child_preds.attrs.get("tier_stats", {})
        context.log.info(
            "downscale_predictions: share tiers — primary=%d, historical_fallback=%d, "
            "uniform=%d",
            tier_stats.get("primary", 0),
            tier_stats.get("historical_fallback", 0),
            tier_stats.get("uniform", 0),
        )
        context.log.info(
            "downscale_predictions: sanity — conservation_max_abs_err=%.3g; "
            "%d parent(s) uniform-split (no-data fallback); "
            "risk vs parent over %d parent-week(s): %d below / %d above / %d match; "
            "sum(predictionInt) vs round_half_up(sum(prediction)): "
            "%d/%d parent-weeks match",
            diag["conservation_max_abs_err"],
            diag["n_parents_uniform"],
            diag["n_parent_weeks"],
            diag["n_weeks_children_below_parent"],
            diag["n_weeks_children_above_parent"],
            diag["n_weeks_zone_match"],
            diag["n_int_conservation_match"],
            diag["n_int_conservation_total"],
        )
        if diag["n_parents_uniform"]:
            context.log.warning(
                "downscale_predictions: %d parent(s) had no case data in the window and "
                "were split uniformly — their child disaggregation is not data-driven",
                diag["n_parents_uniform"],
            )

        state = require_state(context.config)
        child_preds = add_lgd_column(
            child_preds, state=state, spatial_res=cfg.child_level
        )
        dest = context.artifact_path("outputs/predictions.csv")
        context.artifacts.write_text(child_preds.to_csv(index=False), dest)

        context.log.info(
            "downscale_predictions: %d parent rows → %d child rows, written to %s",
            len(parent_preds),
            len(child_preds),
            dest,
        )
        return DownscaleResult(
            output_csv_path=dest,
            n_parent_rows=len(parent_preds),
            n_child_rows=len(child_preds),
            n_parents_uniform=diag["n_parents_uniform"],
            n_weeks_children_below_parent=diag["n_weeks_children_below_parent"],
            n_weeks_children_above_parent=diag["n_weeks_children_above_parent"],
            conservation_max_abs_err=diag["conservation_max_abs_err"],
            n_dropped_parents=len(dropped),
            dropped_parent_ids=tuple(dropped),
        )
