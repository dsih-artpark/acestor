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
    load_region_names,
    render_brief,
)
from pipelines.dengue_downscale.configs import DownscaleConfig
from pipelines.dengue_downscale.results import DownscaleBriefResult, DownscaleResult


@dataclass(frozen=True)
class GenerateDownscaleBriefInputs:
    downscale_predictions: DownscaleResult


class GenerateDownscaleBriefStep(
    BaseStep[GenerateDownscaleBriefInputs, DownscaleBriefResult]
):
    """Render the downscale HTML brief — same template as the dengue pipeline
    plus the diagnostics partial fed by DownscaleResult."""

    input_type: ClassVar[type] = GenerateDownscaleBriefInputs

    def run(
        self,
        context: PipelineContext,
        inputs: GenerateDownscaleBriefInputs,
    ) -> DownscaleBriefResult:
        report_cfg = ReportConfig.from_raw(
            _section(context.config, "report") or {},
            pipeline=_section(context.config, "pipeline"),
        )

        pred_text = context.artifacts.read_text(
            inputs.downscale_predictions.output_csv_path
        )
        df = pd.read_csv(io.StringIO(pred_text))

        primary_model = (
            "ensembleModel"  # downscaler emits one row per parent prediction
        )
        thresh_method = report_cfg.threshold_method_for_report
        report_df = df[
            (df["model"] == primary_model) & (df["thresholdMethod"] == thresh_method)
        ].copy()

        if report_df.empty:
            context.log.warning(
                "generate_downscale_brief: no rows match model=%r thresholdMethod=%r; "
                "rendering empty-state brief",
                primary_model,
                thresh_method,
            )

        charts_dir_fs = Path(context.artifact_fs_path("outputs/charts"))
        charts_dir_fs.mkdir(parents=True, exist_ok=True)

        # Load child-level observed cases for the hero chart (same CSV the
        # downscaler uses for share computation).
        ds_cfg_early = DownscaleConfig.from_raw(context.config.get("downscale") or {})
        observed_df: pd.DataFrame | None = None
        cases_csv_path = Path(ds_cfg_early.cases_csv)
        if cases_csv_path.exists():
            try:
                observed_df = pd.read_csv(cases_csv_path, parse_dates=["date"])
            except Exception as exc:
                context.log.warning(
                    "generate_downscale_brief: failed to load %s: %s",
                    cases_csv_path,
                    exc,
                )

        run_date_ts = pd.Timestamp.now().normalize()

        # Hero forecast chart over all models in the downscaled CSV.
        hero_path = charts_dir_fs / "hero_forecast.png"
        if not df.empty:
            maps_lib.render_hero_forecast(
                df,
                observed_df=observed_df,
                run_date=run_date_ts,
                out_path=str(hero_path),
            )

        # Render one choropleth per predicted week at the child level (e.g. mandal),
        # using the same gen_plot the dengue pipeline uses for districts. Output
        # goes straight into outputs/charts/risk_map_wN.png — same shape the
        # template expects.
        weeks = (
            sorted(report_df["startDatePredictedWeek"].unique())
            if not report_df.empty
            else []
        )
        if weeks:
            geojson_base = ds_cfg_early.geojson_base_path
            for i, wk in enumerate(weeks, start=1):
                thisdate = pd.Timestamp(wk).date().isoformat()
                color_df = report_df[report_df["startDatePredictedWeek"] == wk][
                    ["regionID", "predictionZone"]
                ].copy()
                try:
                    out_png = maps_lib.gen_plot(
                        color_df,
                        region=ds_cfg_early.child_level,
                        model=primary_model,
                        threshold=thresh_method,
                        thisdate=thisdate,
                        geojson_base=geojson_base,
                        output_dir=str(charts_dir_fs),
                        figure_title=f"{ds_cfg_early.child_level.title()} Dengue Risk Map",
                        run_date=str(run_date_ts.date()),
                    )
                    # Rename gen_plot's verbose filename → predictable risk_map_wN.png.
                    target = charts_dir_fs / f"risk_map_w{i}.png"
                    if out_png and Path(out_png).exists():
                        Path(out_png).rename(target)
                except Exception as exc:
                    context.log.warning(
                        "generate_downscale_brief: weekly map render failed for week %s (%s): %s",
                        i,
                        thisdate,
                        exc,
                    )

        diag = {
            "conservation_max_abs_err": inputs.downscale_predictions.conservation_max_abs_err,
            "n_parents_uniform": inputs.downscale_predictions.n_parents_uniform,
            "n_weeks_children_above_parent": inputs.downscale_predictions.n_weeks_children_above_parent,
            "n_weeks_children_below_parent": inputs.downscale_predictions.n_weeks_children_below_parent,
            "n_zone_zero": (
                int((df["predictionZone"] == 0).sum()) if not df.empty else 0
            ),
            "n_total": int(len(df)),
        }

        ds_cfg = DownscaleConfig.from_raw(context.config.get("downscale") or {})

        # Load child-level geojson names (e.g. mandal_05511 → "Hindupur").
        region_names: dict[str, str] = {}
        child_geojson_dir = Path(ds_cfg.geojson_base_path) / ds_cfg.child_level_plural
        if child_geojson_dir.exists():
            region_names = load_region_names(child_geojson_dir)
        else:
            context.log.warning(
                "generate_downscale_brief: child geojson dir not found at %s — table will show raw region IDs",
                child_geojson_dir,
            )

        ctx = build_brief_context(
            predictions=report_df,
            run_date=str(pd.Timestamp.now().date()),
            charts_relpath="charts",
            is_downscale=True,
            document_title=report_cfg.document_title,
            region_type=ds_cfg.child_level,
            parent_region_type=ds_cfg.parent_level,
            region_names=region_names or None,
            downscale_diagnostics=diag,
        )
        html = render_brief(ctx)

        dest_key = context.artifact_path("outputs/report.html")
        context.artifacts.write_text(html, dest_key)
        context.log.info(
            "generate_downscale_brief: html=%s diag=%s",
            dest_key,
            diag,
        )

        return DownscaleBriefResult(
            report_path=dest_key,
            charts_dir=str(charts_dir_fs),
        )
