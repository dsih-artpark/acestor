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
)


@dataclass(frozen=True)
class PrepParseCaseDataInputs:
    download_case_data: PrepCaseDownloadResult


class PrepParseCaseDataStep(BaseStep[PrepParseCaseDataInputs, PrepCaseParseResult]):
    input_type: ClassVar[type] = PrepParseCaseDataInputs

    def run(
        self, context: PipelineContext, inputs: PrepParseCaseDataInputs
    ) -> PrepCaseParseResult:
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
            last_result = PrepCaseParseResult(
                region_type=region_type,
                prepared_data_path=str(dest.resolve()),
                total_rows=total,
            )

        if last_result is None:
            raise ValueError("parse_case_data: no region_types produced output")
        return last_result
