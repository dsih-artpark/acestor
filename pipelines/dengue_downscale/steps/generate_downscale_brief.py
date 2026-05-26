from __future__ import annotations

import io
from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar

import pandas as pd

from acestor import BaseStep, PipelineContext
from pipelines.dengue.configs import ReportConfig, _section
from pipelines.dengue.lib import maps as maps_lib
from pipelines.dengue.lib.brief import build_brief_context, render_brief
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

        # Hero forecast chart over all models in the downscaled CSV.
        hero_path = charts_dir_fs / "hero_forecast.png"
        if not df.empty:
            maps_lib.render_hero_forecast(df, out_path=str(hero_path))

        # Weekly map slots are intentionally left empty for now — mandal-level
        # maps aren't rendered by this pipeline yet (follow-up). The template
        # tolerates missing image src by showing a broken-image icon; that's
        # acceptable signal until mandal maps land.

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

        ctx = build_brief_context(
            predictions=report_df,
            run_date=str(pd.Timestamp.now().date()),
            charts_relpath="charts",
            is_downscale=True,
            document_title=report_cfg.document_title,
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
