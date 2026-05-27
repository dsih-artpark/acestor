from __future__ import annotations

import io
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar

import pandas as pd

from acestor import BaseStep, PipelineContext
from pipelines.dengue.configs import PreparedDataConfig, ReportConfig, _section
from pipelines.dengue.lib import maps as maps_lib
from pipelines.dengue.lib.brief import (
    build_brief_context,
    compute_parent_lookup,
    load_child_geojson_combined,
    load_region_names,
    render_brief,
)
from pipelines.dengue.sources import filesystem as geojson_sources
from pipelines.dengue.results import (
    CutoffDatesResult,
    MapsResult,
    PredictionResult,
    ReportResult,
)


@dataclass(frozen=True)
class GenerateReportInputs:
    train_and_predict: PredictionResult
    generate_maps: MapsResult
    identify_cutoff_dates: CutoffDatesResult


class GenerateReportStep(BaseStep[GenerateReportInputs, ReportResult]):
    """Render the HTML brief replacing the LaTeX report."""

    input_type: ClassVar[type] = GenerateReportInputs

    def run(
        self, context: PipelineContext, inputs: GenerateReportInputs
    ) -> ReportResult:
        cfg = ReportConfig.from_raw(
            _section(context.config, "report"),
            pipeline=_section(context.config, "pipeline"),
        )

        # Read predictions CSV (path written by TrainAndPredictStep — schema agnostic to filename).
        pred_text = context.artifacts.read_text(
            inputs.train_and_predict.predictions_csv_path
        )
        df = pd.read_csv(io.StringIO(pred_text))

        primary_model = maps_lib.MODEL_FULL_NAME.get(cfg.primary, cfg.primary)
        thresh_method = cfg.threshold_method_for_report

        report_df = df[
            (df["model"] == primary_model) & (df["thresholdMethod"] == thresh_method)
        ].copy()

        if report_df.empty:
            context.log.warning(
                "generate_report: no rows in predictions CSV match model=%r thresholdMethod=%r; "
                "rendering an empty-state brief anyway",
                primary_model,
                thresh_method,
            )

        # Ensure outputs/charts/ exists.
        charts_dir_fs = Path(context.artifact_fs_path("outputs/charts"))
        charts_dir_fs.mkdir(parents=True, exist_ok=True)

        # Load observed cases CSV for hero chart enrichment.
        observed_df: pd.DataFrame | None = None
        try:
            pd_cfg = PreparedDataConfig.from_raw(
                _section(context.config, "data.prepared_data")
            )
            region_type = inputs.train_and_predict.region_type or pd_cfg.region_type
            cases_csv_path = Path(pd_cfg.base_dir) / region_type / "cases_daily.csv"
            # Try artifact-relative path first, then as absolute/cwd-relative.
            cases_fs_path = Path(context.artifact_fs_path("..")) / cases_csv_path
            if not cases_fs_path.exists():
                cases_fs_path = Path(pd_cfg.base_dir) / region_type / "cases_daily.csv"
            if cases_fs_path.exists():
                observed_df = pd.read_csv(cases_fs_path, parse_dates=["date"])
            else:
                context.log.warning(
                    "generate_report: cases_daily.csv not found at %s — hero chart will show forecast only",
                    cases_fs_path,
                )
        except Exception as exc:
            context.log.warning(
                "generate_report: failed to load observed cases: %s", exc
            )

        # Load region names from geojsons.
        region_names: dict[str, str] = {}
        try:
            geojson_base = geojson_sources.get_geojson_base_dir()
            if geojson_base:
                region_names = load_region_names(Path(geojson_base))
        except Exception as exc:
            context.log.warning("generate_report: failed to load region names: %s", exc)

        run_date_ts = pd.Timestamp(inputs.identify_cutoff_dates.run_date)

        # Hero forecast chart (uses the full df, not just primary — line per model).
        hero_path = charts_dir_fs / "hero_forecast.png"
        if not df.empty:
            maps_lib.render_hero_forecast(
                df,
                observed_df=observed_df,
                run_date=run_date_ts,
                out_path=str(hero_path),
            )

        # Copy one per-week map from outputs/maps/ → outputs/charts/risk_map_wN.png.
        plots_rel = _section(context.config, "maps").get("output_dir", "plots")
        plots_fs = Path(context.artifact_fs_path(plots_rel))
        weeks = (
            sorted(report_df["startDatePredictedWeek"].unique())
            if not report_df.empty
            else []
        )
        end_str = (
            pd.Timestamp(inputs.identify_cutoff_dates.run_date)
            .date()
            .strftime("%Y%m%d")
        )
        region_token = maps_lib.REGION_LABEL.get(
            inputs.train_and_predict.region_type, inputs.train_and_predict.region_type
        )
        model_token = maps_lib.MODEL_LABEL.get(primary_model, primary_model)
        thresh_token = maps_lib.THRESHOLD_LABEL.get(thresh_method, thresh_method)
        for i, wk in enumerate(weeks, start=1):
            thisdate = pd.Timestamp(wk).date().isoformat()
            src = (
                plots_fs
                / f"{region_token}s_{thisdate}_{model_token}_{thresh_token}_{end_str}.png"
            )
            dst = charts_dir_fs / f"risk_map_w{i}.png"
            if src.exists():
                shutil.copy2(src, dst)
            else:
                context.log.warning(
                    "generate_report: weekly map missing for w%d: %s", i, src.name
                )

        # Build interactive map data (D3) — same shape as the downscale brief.
        interactive_map_data = None
        try:
            geojson_base = geojson_sources.get_geojson_base_dir()
            region_type = inputs.train_and_predict.region_type
            child_dir = Path(geojson_base) / f"{region_type}s"
            if not child_dir.exists():
                child_dir = Path(geojson_base) / region_type
            if child_dir.exists():
                fc = load_child_geojson_combined(child_dir)
                parent_lookup = compute_parent_lookup(fc)
                weekly_zones: dict[str, dict[str, int]] = {}
                if not report_df.empty:
                    weeks_sorted = sorted(report_df["startDatePredictedWeek"].unique())
                    for i, wk in enumerate(weeks_sorted, start=1):
                        sub = report_df[report_df["startDatePredictedWeek"] == wk]
                        weekly_zones[str(i)] = {
                            str(r["regionID"]): int(r["predictionZone"])
                            for _, r in sub.iterrows()
                        }
                interactive_map_data = {
                    "geojson": fc,
                    "parent_lookup": parent_lookup,
                    "weekly_zones": weekly_zones,
                }
        except Exception as exc:
            context.log.warning(
                "generate_report: failed to build interactive map data: %s", exc
            )

        # Parent region type for the dropdown label — first feature's `parent` prefix.
        parent_region_type = "state"
        if interactive_map_data and interactive_map_data["parent_lookup"]:
            any_pid = next(iter(interactive_map_data["parent_lookup"]))
            parent_region_type = any_pid.split("_")[0] or "state"

        ctx = build_brief_context(
            predictions=report_df,
            run_date=str(inputs.identify_cutoff_dates.run_date),
            charts_relpath="charts",
            is_downscale=False,
            document_title=cfg.document_title,
            region_type=inputs.train_and_predict.region_type,
            parent_region_type=parent_region_type,
            region_names=region_names or None,
            interactive_map_data=interactive_map_data,
        )
        html = render_brief(ctx)

        dest_key = context.artifact_path("outputs/report.html")
        context.artifacts.write_text(html, dest_key)
        context.log.info(
            "generate_report: html=%s charts=%s rows=%d",
            dest_key,
            charts_dir_fs,
            len(report_df),
        )

        return ReportResult(
            report_path=dest_key,
            charts_dir=str(charts_dir_fs),
            # Legacy fields stay None — cleanup follows in Task 19.
            pdf_path=None,
            maps_zip_path=None,
            tex_path=None,
            latex_bundle_zip_path=None,
        )
