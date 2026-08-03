"""Parse IHIP-format case files and write daily aggregated counts into prepared_data.

Reads all .xlsx/.xls/.csv files from the configured source folder, aggregates
each row as one confirmed case, maps to region_ids via LGD code or spatial join,
and writes results to prepared_data/<region_type>/cases_daily.csv (full overwrite).

Each run replaces the output file entirely so that filter or config changes are
fully reflected without stale rows from previous runs persisting.

Date filtering:
  - If date_range.start is set in config, only rows >= that date are kept.
  - If date_range.end is set in config, only rows <= that date are kept.
  - If neither is set, all rows from the files are kept.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar

import pandas as pd

from acestor import BaseStep, PipelineContext
from pipelines.dengue_prep.configs import (
    PrepCaseDownloadConfig,
    PrepCaseParseConfig,
    PrepOutputConfig,
    _section,
)
from pipelines.dengue_prep.lib.date_range import resolve_date_range
from pipelines.dengue_prep.lib.ihip import parse_ihip_files
from pipelines.dengue_prep.lib.upsert import write_csv as _write_csv
from pipelines.dengue_prep.results import (
    PrepCaseDownloadResult,
    PrepCaseParseResult,
    PrepGeojsonDownloadResult,
)


@dataclass(frozen=True)
class PrepParseCaseDataInputs:
    download_case_data: PrepCaseDownloadResult
    download_geojsons: PrepGeojsonDownloadResult


class PrepParseCaseDataStep(BaseStep[PrepParseCaseDataInputs, PrepCaseParseResult]):
    input_type: ClassVar[type] = PrepParseCaseDataInputs

    def run(
        self, context: PipelineContext, inputs: PrepParseCaseDataInputs
    ) -> PrepCaseParseResult:
        # Short-circuit when the upstream download step was disabled — some
        # scopes (e.g. Sri Lanka) receive aggregated weekly reports and produce
        # prepared_data/<region_type>/cases_daily.csv out-of-band. In that
        # case there is no IHIP source folder to parse.
        if not inputs.download_case_data.enabled:
            context.log.info(
                "prep parse_case_data: skipped (upstream download_case_data disabled)"
            )
            return PrepCaseParseResult(
                region_type="", prepared_data_path="", total_rows=0
            )

        data_raw = context.config.get("data") or {}
        top_region_type = str(data_raw.get("region_type", "")).strip()

        # Date window is shared with weather download via data.date_range —
        # see pipelines/dengue_prep/lib/date_range.py.
        dr_start, dr_end = resolve_date_range(context.config)
        case_parse_raw = dict(_section(context.config, "data.case_parse"))
        if dr_start:
            case_parse_raw.setdefault("date_start", dr_start)
        if dr_end:
            case_parse_raw.setdefault("date_end", dr_end)
        if top_region_type and not case_parse_raw.get("region_types"):
            case_parse_raw["region_types"] = [top_region_type]

        cfg = PrepCaseParseConfig.from_raw(case_parse_raw)
        out_cfg = PrepOutputConfig.from_raw(
            _section(context.config, "data.prepared_data")
        )
        dl_cfg = PrepCaseDownloadConfig.from_raw(
            _section(context.config, "data.case_download")
        )
        geojson_base = str(
            ((context.config.get("data") or {}).get("geojson") or {}).get(
                "base_path", ""
            )
        ).strip()

        source_folder = dl_cfg.source_path or dl_cfg.filesystem_base_path
        if not source_folder:
            raise ValueError(
                "parse_case_data: no source folder configured. "
                "Set data.case_download.source_path in your config."
            )

        date_start = (
            pd.Timestamp(cfg.date_start).normalize() if cfg.date_start else None
        )
        date_end = pd.Timestamp(cfg.date_end).normalize() if cfg.date_end else None

        last_result: PrepCaseParseResult | None = None

        for region_type in cfg.region_types:
            from pipelines.dengue_prep.lib.summary import CaseParseStats

            stats = CaseParseStats(region_type=region_type)
            daily = parse_ihip_files(
                folder=source_folder,
                region_type=region_type,
                geojson_base=geojson_base,
                date_column=cfg.date_column,
                lgd_code_column=cfg.lgd_code_column or None,
                lat_column=cfg.lat_column,
                lon_column=cfg.lon_column,
                filters=cfg.filters or None,
                geocoding_cfg=cfg.geocoding,
                header_row=cfg.header_row,
                region_id_column=cfg.region_id_column or None,
                stats=stats,
            )

            # Apply date range filter — only if set in config
            daily["date"] = pd.to_datetime(daily["date"])
            n_before_filter = len(daily)
            if date_start is not None:
                daily = daily[daily["date"] >= date_start]
            if date_end is not None:
                daily = daily[daily["date"] <= date_end]
            if date_start is not None or date_end is not None:
                context.log.info(
                    "parse_case_data: region_type=%s date filter [%s → %s]: "
                    "%d rows → %d rows (dropped %d)",
                    region_type,
                    date_start.date() if date_start else "unbounded",
                    date_end.date() if date_end else "unbounded",
                    n_before_filter,
                    len(daily),
                    n_before_filter - len(daily),
                )

            if daily.empty:
                context.log.warning(
                    "parse_case_data: no rows remain for region_type=%s after date filter "
                    "(%s → %s)",
                    region_type,
                    date_start,
                    date_end,
                )

            dest = Path(out_cfg.base_dir) / region_type / "cases_daily.csv"
            total = _write_csv(dest, daily, key_cols=["region_id", "date"])
            context.log.info(
                "parse_case_data: region_type=%s parsed %d rows, wrote → %d rows in %s",
                region_type,
                len(daily),
                total,
                dest,
            )

            # ── End-of-step summary — rich table to terminal + markdown record ──
            from pipelines.dengue_prep.lib.ihip import (
                _region_id_to_name,
                _scan_geojson_hierarchy,
                _valid_region_ids,
                _walk_hierarchy,
            )
            from pipelines.dengue_prep.lib.summary import (
                render_case_parse_summary,
                write_markdown_report,
            )

            stats.output_path = str(dest.resolve())
            stats.output_rows = total
            stats.target_region_ids = _valid_region_ids(geojson_base, region_type)
            stats.region_names = _region_id_to_name(geojson_base, region_type)
            _hier = _scan_geojson_hierarchy(geojson_base)
            stats.region_hierarchy = {
                rid: _walk_hierarchy(rid, _hier) for rid in stats.target_region_ids
            }
            if not daily.empty:
                stats.covered_region_ids = set(daily["region_id"].astype(str).unique())
                stats.latest_date = pd.Timestamp(daily["date"].max())
                per_region = daily.groupby("region_id").agg(
                    latest=("date", "max"),
                    cases=("case_count", "sum"),
                )
                stats.per_region_latest = {
                    str(k): pd.Timestamp(v) for k, v in per_region["latest"].items()
                }
                stats.per_region_case_count = {
                    str(k): int(v) for k, v in per_region["cases"].items()
                }
            run_date_ts = (
                pd.Timestamp(context.config.get("run", {}).get("run_date"))
                if context.config.get("run", {}).get("run_date")
                else pd.Timestamp.now().normalize()
            )
            render_case_parse_summary(stats, run_date=run_date_ts)
            report_path = Path(context.artifact_fs_path("outputs/prep_summary.md"))
            write_markdown_report(report_path, case=stats, run_date=run_date_ts)
            context.log.info("parse_case_data: summary written to %s", report_path)

            last_result = PrepCaseParseResult(
                region_type=region_type,
                prepared_data_path=str(dest.resolve()),
                total_rows=total,
            )

        if last_result is None:
            raise ValueError("parse_case_data: no region_types produced output")
        return last_result
