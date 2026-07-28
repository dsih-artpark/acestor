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


def _emit_summary(
    *,
    context,
    cfg,
    classification_method: str,
    source_run_id: str,
    parent_preds: pd.DataFrame,
    child_preds: pd.DataFrame,
    child_mapping: dict[str, str],
    cases_df: pd.DataFrame,
    as_of_date,
    diag: dict,
    output_path: str,
) -> None:
    """Collect stats + render rich terminal summary + append to markdown report.

    Kept out of the main run() body to keep the step method readable; all
    inputs are already computed by the time this fires.
    """
    from pipelines.dengue_downscale.lib.summary import (
        DownscaleStats,
        ParentShareStats,
        render_downscale_summary,
        write_markdown_report,
    )

    # Resolve region names from the child geojson.
    child_names: dict[str, str] = {}
    parent_names: dict[str, str] = {}
    try:
        import geopandas as gpd

        base = Path(cfg.geojson_base_path)
        for f in (base / cfg.child_level_plural).rglob("*.geojson"):
            try:
                g = gpd.read_file(f)
            except Exception:
                continue
            for _, r in g.iterrows():
                rid = r.get("region_id")
                if pd.notna(rid) and pd.notna(r.get("name", None)):
                    child_names[str(rid)] = str(r["name"])
        for f in (base / (cfg.parent_level + "s")).rglob("*.geojson"):
            try:
                g = gpd.read_file(f)
            except Exception:
                continue
            for _, r in g.iterrows():
                rid = r.get("region_id")
                if pd.notna(rid) and pd.notna(r.get("name", None)):
                    parent_names[str(rid)] = str(r["name"])
    except Exception:
        pass

    stats = DownscaleStats(
        parent_level=cfg.parent_level,
        child_level=cfg.child_level,
        source_run_id=source_run_id,
        as_of_date=pd.Timestamp(as_of_date) if as_of_date is not None else None,
        window_weeks=cfg.window_weeks,
        classification_method=classification_method,
        n_parent_rows=len(parent_preds),
        n_child_rows=len(child_preds),
        n_children_in_mapping=len(child_mapping),
        conservation_max_abs_err=float(diag.get("conservation_max_abs_err", 0.0)),
        n_parent_weeks=int(diag.get("n_parent_weeks", 0)),
        n_parents_uniform=int(diag.get("n_parents_uniform", 0)),
        n_children_below_parent=int(diag.get("n_weeks_children_below_parent", 0)),
        n_children_above_parent=int(diag.get("n_weeks_children_above_parent", 0)),
        n_children_match_parent=int(diag.get("n_weeks_zone_match", 0)),
        output_path=output_path,
    )

    # Source cutoff = latest date in parent predictions.
    if "startDatePredictedWeek" in parent_preds.columns:
        try:
            stats.source_cutoff_date = pd.Timestamp(
                pd.to_datetime(parent_preds["startDatePredictedWeek"]).min()
            )
            stats.source_freshness_days = (
                pd.Timestamp.now().normalize() - stats.source_cutoff_date.normalize()
            ).days
        except Exception:
            pass

    # Per-parent share breakdown.
    # Two distinct concepts:
    #   1. "Recent cases in window" — feeds the uniform-fallback decision per
    #      parent. Zero recent cases → that parent's children get split evenly.
    #   2. "Orphan child" — a child region present in the mapping that never
    #      appears in cases_daily.csv AT ALL (any date). Structural data gap.
    case_col = "case_count" if "case_count" in cases_df.columns else "case"
    window_start = pd.Timestamp(as_of_date) - pd.Timedelta(weeks=cfg.window_weeks)
    recent = cases_df[cases_df["date"] > window_start]
    per_child_recent = recent.groupby("region_id")[case_col].sum().to_dict()
    all_seen_regions = set(cases_df["region_id"].astype(str).unique())

    parents_seen: dict[str, list[str]] = {}
    for child_id, parent_id in child_mapping.items():
        parents_seen.setdefault(parent_id, []).append(child_id)

    orphans_all: dict[str, str] = {}
    for parent_id, child_ids in parents_seen.items():
        with_recent = sum(1 for c in child_ids if per_child_recent.get(c, 0) > 0)
        no_history = [c for c in child_ids if c not in all_seen_regions]
        total = int(sum(per_child_recent.get(c, 0) for c in child_ids))
        uniform = total == 0
        stats.per_parent.append(
            ParentShareStats(
                parent_id=parent_id,
                parent_name=parent_names.get(parent_id, ""),
                n_children=len(child_ids),
                n_children_with_cases=with_recent,
                n_children_missing_from_cases=len(no_history),
                orphan_children=no_history,
                total_cases_in_window=total,
                uniform_fallback=uniform,
            )
        )
        for m in no_history:
            orphans_all[m] = child_names.get(m, "?")
    stats.orphan_children_names = orphans_all

    # geojson child count = union of all mapped children (already discovered)
    # + any orphans not in mapping — approximate via mapping count.
    stats.n_children_in_geojson = (
        len(child_names) if child_names else len(child_mapping)
    )

    # Zone-zero counts per (model, thresholdMethod, target_week)
    if not child_preds.empty and "predictionZone" in child_preds.columns:
        grp = child_preds.groupby(
            ["model", "thresholdMethod", "startDatePredictedWeek"]
        )
        for key, sub in grp:
            key = (str(key[0]), str(key[1]), str(key[2]))
            stats.n_total_by_key[key] = len(sub)
            z0 = int((pd.to_numeric(sub["predictionZone"], errors="coerce") == 0).sum())
            stats.n_zone_zero_by_key[key] = z0

    run_date_ts = (
        pd.Timestamp(context.config.get("run", {}).get("run_date"))
        if context.config.get("run", {}).get("run_date")
        else pd.Timestamp.now().normalize()
    )
    render_downscale_summary(stats, run_date=run_date_ts)
    report_path = Path(context.artifact_fs_path("outputs/prep_summary.md"))
    write_markdown_report(report_path, downscale=stats, run_date=run_date_ts)
    context.log.info("downscale_predictions: summary written to %s", report_path)


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
        # CSV-boundary reverse rename: parent CSVs from the main pipeline expose
        # `prediction` = display int and `predictionRaw` = the model float. All
        # internal downscale math needs the float, so swap them back on read.
        # Old parent CSVs (no predictionRaw column) pass through unchanged for
        # backward compatibility.
        if "predictionRaw" in parent_preds.columns:
            # Drop pre-existing predictionInt (redundant with prediction at the
            # CSV boundary) before the swap, otherwise the rename produces
            # duplicate `predictionInt` columns and the reverse rename before
            # writing produces a duplicate `prediction` in the output header.
            if "predictionInt" in parent_preds.columns:
                parent_preds = parent_preds.drop(columns=["predictionInt"])
            parent_preds = parent_preds.rename(
                columns={"prediction": "predictionInt", "predictionRaw": "prediction"}
            )

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
        # CSV-boundary rename — same convention as the main pipeline: `prediction`
        # in the CSV is the display integer; `predictionRaw` is the model float.
        child_preds = child_preds.rename(
            columns={"prediction": "predictionRaw", "predictionInt": "prediction"}
        )
        dest = context.artifact_path("outputs/predictions.csv")
        context.artifacts.write_text(child_preds.to_csv(index=False), dest)

        context.log.info(
            "downscale_predictions: %d parent rows → %d child rows, written to %s",
            len(parent_preds),
            len(child_preds),
            dest,
        )

        # ── End-of-step summary — rich terminal + markdown ───────────────────
        _emit_summary(
            context=context,
            cfg=cfg,
            classification_method=classification_method,
            source_run_id=inputs.load_predictions.source_run_id,
            parent_preds=parent_preds,
            child_preds=child_preds,
            child_mapping=child_mapping,
            cases_df=cases_df,
            as_of_date=as_of_date,
            diag=diag,
            output_path=str(context.artifact_fs_path("outputs/predictions.csv")),
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
