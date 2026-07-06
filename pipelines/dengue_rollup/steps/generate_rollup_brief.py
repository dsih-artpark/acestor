"""Render the rollup HTML brief — mirrors the downscale brief but at the
parent (rolled up) level. Same template + hero forecast chart + interactive
D3 map.

The key differences from the downscale brief:
  * "child level" here = the target (parent) — e.g. corp
  * "parent level" is not shown (nothing above the rolled-up target in this
    hierarchy view)
  * Diagnostics are simpler — the rollup sum invariant is trivially exact
    at the raw-float level, so we only surface the source→target row counts
    and any orphaned children.
"""

from __future__ import annotations

import io
from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar

import pandas as pd

from acestor import BaseStep, PipelineContext
from pipelines.dengue.configs import ReportConfig, _section
from pipelines.dengue.lib import maps as maps_lib
from pipelines.dengue.lib.brief import (
    build_brief_context,
    compute_parent_lookup,
    load_child_geojson_combined,
    load_region_names,
    render_brief,
)
from pipelines.dengue_rollup.configs import RollupConfig
from pipelines.dengue_rollup.results import RollupBriefResult, RollupResult


@dataclass(frozen=True)
class GenerateRollupBriefInputs:
    rollup_predictions: RollupResult


class GenerateRollupBriefStep(BaseStep[GenerateRollupBriefInputs, RollupBriefResult]):
    input_type: ClassVar[type] = GenerateRollupBriefInputs

    def run(
        self,
        context: PipelineContext,
        inputs: GenerateRollupBriefInputs,
    ) -> RollupBriefResult:
        report_cfg = ReportConfig.from_raw(
            _section(context.config, "report") or {},
            pipeline=_section(context.config, "pipeline"),
        )

        pred_text = context.artifacts.read_text(
            inputs.rollup_predictions.output_csv_path
        )
        df = pd.read_csv(io.StringIO(pred_text))

        primary_model = "ensembleModel"
        thresh_method = report_cfg.threshold_method_for_report
        report_df = df[
            (df["model"] == primary_model) & (df["thresholdMethod"] == thresh_method)
        ].copy()

        if report_df.empty:
            context.log.warning(
                "generate_rollup_brief: no rows match model=%r thresholdMethod=%r; "
                "rendering empty-state brief",
                primary_model,
                thresh_method,
            )

        charts_dir_fs = Path(context.artifact_fs_path("outputs/charts"))
        charts_dir_fs.mkdir(parents=True, exist_ok=True)

        up_cfg = RollupConfig.from_raw(context.config.get("rollup") or {})
        observed_df: pd.DataFrame | None = None
        cases_csv_path = Path(up_cfg.cases_csv)
        if cases_csv_path.exists():
            try:
                observed_df = pd.read_csv(cases_csv_path, parse_dates=["date"])
            except Exception as exc:
                context.log.warning(
                    "generate_rollup_brief: failed to load %s: %s",
                    cases_csv_path,
                    exc,
                )

        run_date_ts = pd.Timestamp.now().normalize()

        hero_path = charts_dir_fs / "hero_forecast.png"
        if not df.empty:
            maps_lib.render_hero_forecast(
                df,
                observed_df=observed_df,
                run_date=run_date_ts,
                out_path=str(hero_path),
            )

        weeks = (
            sorted(report_df["startDatePredictedWeek"].unique())
            if not report_df.empty
            else []
        )

        diag = {
            "n_source_rows": inputs.rollup_predictions.n_source_rows,
            "n_target_rows": inputs.rollup_predictions.n_target_rows,
            "n_parents_missing_children": (
                inputs.rollup_predictions.n_parents_missing_children
            ),
            "n_zone_zero": (
                int((df["predictionZone"] == 0).sum()) if not df.empty else 0
            ),
            "n_total": int(len(df)),
        }

        # Parent-level (target of rollup) geojson for region names + the map.
        region_names: dict[str, str] = {}
        target_geojson_dir = Path(up_cfg.geojson_base_path) / up_cfg.target_level_plural
        if target_geojson_dir.exists():
            region_names.update(load_region_names(target_geojson_dir))
        else:
            context.log.warning(
                "generate_rollup_brief: target geojson dir not found at %s "
                "— table will show raw region IDs",
                target_geojson_dir,
            )

        interactive_map_data: dict | None = None
        if target_geojson_dir.exists():
            try:
                geojson_fc = load_child_geojson_combined(
                    target_geojson_dir, simplify_tolerance=0.001
                )
                parent_lookup = compute_parent_lookup(
                    geojson_fc, region_names=region_names
                )
                weekly_zones: dict[str, dict[str, int]] = {}
                for i, wk in enumerate(weeks, start=1):
                    sub = report_df[report_df["startDatePredictedWeek"] == wk]
                    weekly_zones[str(i)] = {
                        str(r["regionID"]): int(r["predictionZone"])
                        for _, r in sub.iterrows()
                    }
                interactive_map_data = {
                    "geojson": geojson_fc,
                    "parent_lookup": parent_lookup,
                    "weekly_zones": weekly_zones,
                }
                context.log.info(
                    "generate_rollup_brief: interactive map built — "
                    "%d features, %d parents",
                    len(geojson_fc["features"]),
                    len(parent_lookup),
                )
            except Exception as exc:
                context.log.warning(
                    "generate_rollup_brief: interactive map build failed "
                    "(%s) — falling back to no map",
                    exc,
                )

        # `parent_region_type` label above the target level. For rollup
        # there's no drill-up, so we surface the scope/state as the umbrella.
        ctx = build_brief_context(
            predictions=report_df,
            run_date=str(pd.Timestamp.now().date()),
            charts_relpath="charts",
            is_downscale=False,
            document_title=report_cfg.document_title,
            region_type=up_cfg.target_level,
            parent_region_type="scope",
            region_names=region_names or None,
            downscale_diagnostics=diag,
            interactive_map_data=interactive_map_data,
        )
        html = render_brief(ctx)

        dest_key = context.artifact_path("outputs/report.html")
        context.artifacts.write_text(html, dest_key)
        context.log.info(
            "generate_rollup_brief: html=%s diag=%s",
            dest_key,
            diag,
        )

        return RollupBriefResult(
            report_path=dest_key,
            charts_dir=str(charts_dir_fs),
        )
