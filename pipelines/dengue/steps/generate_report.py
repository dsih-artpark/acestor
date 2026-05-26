from __future__ import annotations

import io
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar

import pandas as pd

from acestor import BaseStep, PipelineContext
from pipelines.dengue.configs import ReportConfig, _section
from pipelines.dengue.lib import maps as maps_lib
from pipelines.dengue.lib.brief import build_brief_context, render_brief
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

        primary_model = "ensembleModel" if cfg.primary == "ensemble" else cfg.primary
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

        # Hero forecast chart (uses the full df, not just primary — line per model).
        hero_path = charts_dir_fs / "hero_forecast.png"
        if not df.empty:
            maps_lib.render_hero_forecast(df, out_path=str(hero_path))

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

        ctx = build_brief_context(
            predictions=report_df,
            run_date=str(inputs.identify_cutoff_dates.run_date),
            charts_relpath="charts",
            is_downscale=False,
            document_title=cfg.document_title,
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
