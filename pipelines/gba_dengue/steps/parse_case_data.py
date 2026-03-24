from __future__ import annotations

import io
import os
from dataclasses import dataclass
from typing import ClassVar

import pandas as pd

from acestor import BaseStep, PipelineContext
from acestor.core.sources import FileSystemSource, S3Source
from pipelines.gba_dengue.configs import CaseDownloadConfig, CaseParseConfig, _section
from pipelines.gba_dengue.lib import case_data
from pipelines.gba_dengue.sources import filesystem as geojson_sources
from pipelines.gba_dengue.results import (
    CaseDownloadResult,
    ParseCaseDataResult,
    SamplingDayResult,
)

# Columns that identify raw non-standardized lab data
_RAW_NONSTANDARD_COLS = {
    "Sample Collected Date",
    "lab_address_geocoded_long",
    "lab_address_geocoded_lat",
}

# Columns present in pre-aggregated zone-level data (already geocoded + aggregated)
_PRE_AGGREGATED_COLS = {"region_id", "date", "case"}

# Spatial join common_cols, w_params, operations (fixed for case data)
_COMMON_COLS = ["region_id", "name", "parent", "parent_name"]
_W_PARAMS = ["case"]
_OPERATIONS = ["sum"]


@dataclass(frozen=True)
class ParseCaseDataInputs:
    identify_sampling_day: SamplingDayResult
    download_case_data: CaseDownloadResult


class ParseCaseDataStep(BaseStep[ParseCaseDataInputs, ParseCaseDataResult]):
    input_type: ClassVar[type] = ParseCaseDataInputs

    def _build_case_source(self, cfg: CaseDownloadConfig):
        backend = (cfg.source_backend or "filesystem").strip().lower()
        source_path = (cfg.source_path or "").strip()
        if source_path and backend == "s3" and source_path.startswith("s3://"):
            bucket_and_prefix = source_path[len("s3://") :]
            bucket, _, prefix = bucket_and_prefix.partition("/")
            return S3Source(
                bucket=bucket,
                base_prefix=prefix,
                aws_profile=(os.getenv("AWS_PROFILE", "").strip() or None),
                region=(os.getenv("AWS_REGION", "").strip() or None),
                cache_enabled=cfg.cache_enabled,
                cache_dir=cfg.cache_dir,
                strategy=cfg.cache_strategy,
            )
        if source_path and backend == "filesystem":
            return FileSystemSource(base_path=source_path)
        if backend == "s3":
            return S3Source(
                bucket=cfg.s3_bucket,
                base_prefix=cfg.s3_prefix,
                aws_profile=(os.getenv("AWS_PROFILE", "").strip() or None),
                region=(os.getenv("AWS_REGION", "").strip() or None),
                cache_enabled=cfg.cache_enabled,
                cache_dir=cfg.cache_dir,
                strategy=cfg.cache_strategy,
            )
        return FileSystemSource(base_path=cfg.filesystem_base_path)

    def _read_case_bytes(self, context: PipelineContext, source, ref: str) -> bytes:
        if ref.startswith("filesystem://"):
            return source.read(ref[len("filesystem://") :])
        if ref.startswith("fs://"):
            return source.read(ref[len("fs://") :])
        if ref.startswith("s3://"):
            return source.read(ref[len("s3://") :])
        # Backward compatibility for older runs that wrote artifact keys.
        return context.artifacts.read(ref)

    def run(
        self, context: PipelineContext, inputs: ParseCaseDataInputs
    ) -> ParseCaseDataResult:
        cfg = CaseParseConfig.from_raw(_section(context.config, "data.case_parse"))

        if not inputs.download_case_data.copied_files:
            raise ValueError(
                "parse_case_data requires case files; download_case_data produced none. "
                "Enable case_download and set source paths so files are available."
            )

        case_dl_cfg = CaseDownloadConfig.from_raw(
            _section(context.config, "data.case_download")
        )
        case_source = self._build_case_source(case_dl_cfg)
        raw_dfs = [
            pd.read_csv(
                io.BytesIO(self._read_case_bytes(context, case_source, f)),
                low_memory=False,
            )
            for f in inputs.download_case_data.copied_files
        ]

        first_cols = set(raw_dfs[0].columns)

        if _PRE_AGGREGATED_COLS.issubset(first_cols):
            if len(cfg.region_types) != 1:
                raise ValueError(
                    "Pre-aggregated case input (region_id/date/case) only supports exactly one entry in "
                    "data.case_parse.region_types - the file is already at a fixed spatial resolution. "
                    f"Got region_types={cfg.region_types!r}."
                )
            context.log.info(
                "parse_case_data: detected pre-aggregated daily data (region_id/date/case), "
                "skipping spatial join (region_type=%s)",
                cfg.region_types[0],
            )
            daily = pd.concat(raw_dfs, ignore_index=True).copy()
            daily["date"] = pd.to_datetime(daily["date"])
            daily = daily[["region_id", "date", "case"]].copy()
            by_region = self._finalize_and_write(
                context, inputs, daily, cfg.region_types[0]
            )
        elif _RAW_NONSTANDARD_COLS.issubset(first_cols):
            context.log.info(
                "parse_case_data: detected raw non-standardized lab data (lat/lon), "
                "performing spatial join geocoding for region_types=%s",
                cfg.region_types,
            )
            date_start = (
                pd.Timestamp(cfg.date_start).normalize() if cfg.date_start else None
            )
            date_end = (
                pd.Timestamp(cfg.date_end).normalize()
                if cfg.date_end
                else pd.Timestamp.today().normalize()
            )

            by_region: dict[str, str] = {}
            for region_type in cfg.region_types:
                gdf = case_data.load_region_gdf(
                    geojson_sources.get_geojson_base_dir(), region_type
                )
                per_file = case_data.aggregate_all_data_daily(
                    raw_dfs,
                    gdf,
                    _COMMON_COLS,
                    _W_PARAMS,
                    _OPERATIONS,
                    date_start=date_start,
                    date_end=date_end,
                )
                if not per_file:
                    raise ValueError(
                        f"aggregate_all_data_daily produced no output for region_type={region_type!r} - "
                        "check region_type and that case CSVs have valid lat/lon."
                    )
                daily_rt = case_data.concat_daily_dfs(per_file, _COMMON_COLS, _W_PARAMS)
                by_region.update(
                    self._finalize_and_write(context, inputs, daily_rt, region_type)
                )
        else:
            raise ValueError(
                f"Unrecognised case data format. Expected either pre-aggregated columns "
                f"{_PRE_AGGREGATED_COLS} or raw non-standardized columns {_RAW_NONSTANDARD_COLS}. "
                f"Got: {first_cols}"
            )

        # Single downstream case path: last region_type in config order (matches SOT: corp then zone).
        driver_rt = cfg.region_types[-1]
        driver_path = by_region.get(driver_rt)
        if driver_path is None:
            raise ValueError(
                f"Expected output for last region_types entry {driver_rt!r}; got {list(by_region)}"
            )

        return ParseCaseDataResult(
            sampled_csv_path=driver_path,
            region_type=driver_rt,
            sampled_by_region_type=dict(by_region),
        )

    def _finalize_and_write(
        self,
        context: PipelineContext,
        inputs: ParseCaseDataInputs,
        daily: pd.DataFrame,
        region_type: str,
    ) -> dict[str, str]:
        daily_dest = context.artifact_path(f"datasets/cases_{region_type}_daily.csv")
        context.artifacts.write_text(daily.to_csv(index=False), daily_dest)

        rolling = case_data.rolling_aggregate(daily, n_days=7)
        max_date = pd.Timestamp(rolling["date"].max())
        latest_day = case_data.get_latest_sampling_day(
            max_date, inputs.identify_sampling_day.sampling_day
        )
        sampled = case_data.sample_data(rolling, end_date=latest_day)
        renamed = case_data.rename_columns_for_output(sampled, region_type)
        sampled_dest = context.artifact_path(
            f"datasets/cases_{region_type}_sampled.csv"
        )
        context.artifacts.write_text(renamed.to_csv(index=False), sampled_dest)
        context.log.info(
            "parse_case_data: region_type=%s daily_rows=%d sampled_rows=%d",
            region_type,
            len(daily),
            len(renamed),
        )
        return {region_type: sampled_dest}
