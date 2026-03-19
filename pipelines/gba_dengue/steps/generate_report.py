from __future__ import annotations

import io
from dataclasses import dataclass
from typing import ClassVar

import pandas as pd

from acestor import BaseStep, PipelineContext
from pipelines.gba_dengue.configs import ReportConfig, _section
from pipelines.gba_dengue.lib import report
from pipelines.gba_dengue.results import (
    MapsResult,
    ReportResult,
    ThresholdAssessmentResult,
)


@dataclass(frozen=True)
class GenerateReportInputs:
    assess_thresholds: ThresholdAssessmentResult
    generate_maps: MapsResult


class GenerateReportStep(BaseStep[GenerateReportInputs, ReportResult]):
    input_type: ClassVar[type] = GenerateReportInputs

    def run(
        self, context: PipelineContext, inputs: GenerateReportInputs
    ) -> ReportResult:
        cfg = ReportConfig.from_raw(_section(context.config, "report"))

        corp_csv = context.artifacts.read_text(
            inputs.assess_thresholds.best_method_corp_csv
        )
        zone_csv = context.artifacts.read_text(
            inputs.assess_thresholds.best_method_zone_csv
        )
        best_corp = pd.read_csv(io.StringIO(corp_csv))
        best_zone = pd.read_csv(io.StringIO(zone_csv))

        details_corp = report.get_relevant_figures_details(best_corp)
        details_zone = report.get_relevant_figures_details(best_zone)

        report_path = context.artifact_path(f"{cfg.output_dir}/report.pdf")
        context.log.info(
            "generate_report: corp_figs=%d zone_figs=%d output=%s",
            len(details_corp[2]) if details_corp else 0,
            len(details_zone[2]) if details_zone else 0,
            report_path,
        )

        return ReportResult(report_path=report_path)
