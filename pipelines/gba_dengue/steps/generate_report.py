from __future__ import annotations

import io
import json
from dataclasses import dataclass
from typing import ClassVar

import pandas as pd

from acestor import BaseStep, PipelineContext
from pipelines.gba_dengue.configs import ReportConfig, _section
from pipelines.gba_dengue.lib import report as report_lib
from pipelines.gba_dengue.results import (
    CutoffDatesResult,
    MapsResult,
    ReportResult,
    ThresholdAssessmentResult,
)


@dataclass(frozen=True)
class GenerateReportInputs:
    assess_thresholds: ThresholdAssessmentResult
    generate_maps: MapsResult
    identify_cutoff_dates: CutoffDatesResult


class GenerateReportStep(BaseStep[GenerateReportInputs, ReportResult]):
    input_type: ClassVar[type] = GenerateReportInputs

    def run(
        self, context: PipelineContext, inputs: GenerateReportInputs
    ) -> ReportResult:
        cfg = ReportConfig.from_raw(
            _section(context.config, "report"),
            pipeline=_section(context.config, "pipeline"),
        )
        maps_raw = _section(context.config, "maps")
        plots_rel = maps_raw.get("output_dir", "plots")
        data_raw = _section(context.config, "data")
        case_parse = data_raw.get("case_parse") or {}
        epi_start = (
            str(case_parse.get("date_start", "2021-11-09")).strip() or "2021-11-09"
        )

        corp_csv = context.artifacts.read_text(
            inputs.assess_thresholds.best_method_corp_csv
        )
        zone_csv = context.artifacts.read_text(
            inputs.assess_thresholds.best_method_zone_csv
        )
        best_corp = (
            pd.read_csv(io.StringIO(corp_csv)) if corp_csv.strip() else pd.DataFrame()
        )
        best_zone = (
            pd.read_csv(io.StringIO(zone_csv)) if zone_csv.strip() else pd.DataFrame()
        )

        ref_date = pd.Timestamp.today().normalize()
        corp_details = report_lib.get_relevant_figures_details(best_corp, ref_date)
        zone_details = report_lib.get_relevant_figures_details(best_zone, ref_date)

        pred_c = dates_c = fnames_c = caps_c = None
        if corp_details:
            pred_c, dates_c, fnames_c, caps_c = corp_details
            caps_c = report_lib.postprocess_captions_for_rep(
                caps_c,
                kind="corp",
                caption_corp_scope=cfg.caption_primary,
                caption_zone_scope=cfg.caption_secondary,
            )

        pred_z = dates_z = fnames_z = caps_z = None
        if zone_details:
            pred_z, dates_z, fnames_z, caps_z = zone_details
            caps_z = report_lib.postprocess_captions_for_rep(
                caps_z,
                kind="zone",
                caption_corp_scope=cfg.caption_primary,
                caption_zone_scope=cfg.caption_secondary,
            )

        co = inputs.identify_cutoff_dates
        rep_dict = report_lib.build_rep_dict(
            pred_c=pred_c,
            dates_c=list(dates_c) if dates_c is not None else None,
            fnames_c=list(fnames_c) if fnames_c is not None else None,
            captions_c=list(caps_c) if caps_c is not None else None,
            pred_z=pred_z,
            dates_z=list(dates_z) if dates_z is not None else None,
            fnames_z=list(fnames_z) if fnames_z is not None else None,
            captions_z=list(caps_z) if caps_z is not None else None,
            cutoff_case=co.cutoff_case,
            cutoff_weather=co.cutoff_weather,
            epi_data_start_date=epi_start,
        )

        end_str = pd.Timestamp.today().date().strftime("%Y%m%d")
        month_key = rep_dict["reportmonth"] or "report"
        safe_month = month_key.replace(" ", "_").replace("/", "-")

        pred_raw = rep_dict.get("prediction_date") or str(pd.Timestamp.today().date())
        access_date = (
            pd.Timestamp(str(pred_raw).replace("--", "-")).date().strftime("%d-%b-%Y")
        )

        rep_json = context.artifact_path(f"{cfg.output_dir}/rep_dict_{end_str}.json")
        context.artifacts.write_text(
            json.dumps(rep_dict, indent=2, default=str) + "\n",
            rep_json,
        )

        plots_dir = context.artifact_fs_path(plots_rel)
        all_maps_zip_path = context.artifact_fs_path(
            f"results/AllMaps_{safe_month}_{end_str}.zip"
        )
        all_names = (list(fnames_c) if fnames_c else []) + (
            list(fnames_z) if fnames_z else []
        )
        if all_names:
            report_lib.zip_map_files(
                plots_dir=plots_dir,
                filenames=all_names,
                destination_zip=all_maps_zip_path,
            )

        tex_fs = context.artifact_fs_path(
            f"{cfg.output_dir}/report_summary_{end_str}.tex"
        )
        report_lib.write_minimal_pdf_source(
            rep_dict, tex_fs, document_title=cfg.document_title
        )
        tex_key = context.artifact_path(
            f"{cfg.output_dir}/report_summary_{end_str}.tex"
        )

        bundle_token = report_lib.safe_bundle_filename_prefix(cfg.bundle_prefix)
        bundle_fs = context.artifact_fs_path(
            f"results/{bundle_token}_{safe_month}_{end_str}.zip"
        )
        report_lib.create_latex_bundle_zip(
            rep_dict=rep_dict,
            access_date=access_date,
            plots_dir=plots_dir,
            image_filenames=all_names,
            destination_zip=bundle_fs,
        )
        latex_bundle_key = context.artifact_path(
            f"results/{bundle_token}_{safe_month}_{end_str}.zip"
        )

        details_corp = corp_details
        details_zone = zone_details
        pdf_path: str | None = None
        if cfg.compile_pdf:
            dest_pdf = context.artifact_fs_path(
                f"results/Report_{safe_month}_{end_str}.pdf"
            )
            pdf = report_lib.compile_latex_bundle_zip(
                bundle_fs,
                destination_pdf_path=dest_pdf,
                latex_bin="pdflatex",
            )
            if pdf is not None:
                pdf_path = str(pdf)
                context.log.info("generate_report: pdf=%s", pdf_path)
            else:
                context.log.warning(
                    "generate_report: compile_pdf=true but pdflatex failed (is LaTeX installed?)",
                )

        context.log.info(
            "generate_report: corp_details=%s zone_details=%s maps_zipped=%d rep_dict=%s tex=%s latex_zip=%s",
            details_corp is not None,
            details_zone is not None,
            len(all_names),
            rep_json,
            tex_key,
            latex_bundle_key,
        )

        return ReportResult(
            report_path=rep_json,
            pdf_path=pdf_path,
            tex_path=tex_key,
            latex_bundle_zip_path=latex_bundle_key,
        )
