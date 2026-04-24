"""Parse weather CSVs and write daily aggregated data into prepared_data.

Each run replaces the output file entirely. Columns are stored with original
ERA5 names (2mTemperature, etc.) so that Pipeline 2 (dengue) can apply
rolling aggregation before renaming.
"""

from __future__ import annotations

import io
import os
from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar

import pandas as pd

from acestor import BaseStep, PipelineContext
from acestor.core.sources import FileSystemSource, S3Source
from pipelines.dengue.lib import weather
from pipelines.dengue_prep.configs import (
    PrepOutputConfig,
    PrepWeatherDownloadConfig,
    PrepWeatherParseConfig,
    _section,
)
from pipelines.dengue_prep.lib.upsert import write_csv as _write_csv
from pipelines.dengue_prep.results import (
    PrepWeatherDownloadResult,
    PrepWeatherParseResult,
)


@dataclass(frozen=True)
class PrepParseWeatherDataInputs:
    download_weather_data: PrepWeatherDownloadResult


class PrepParseWeatherDataStep(
    BaseStep[PrepParseWeatherDataInputs, PrepWeatherParseResult]
):
    input_type: ClassVar[type] = PrepParseWeatherDataInputs

    def _build_weather_source(self, cfg: PrepWeatherDownloadConfig):
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
        if backend == "s3" and cfg.s3_bucket:
            return S3Source(
                bucket=cfg.s3_bucket,
                base_prefix=cfg.s3_prefix,
                aws_profile=(os.getenv("AWS_PROFILE", "").strip() or None),
                region=(os.getenv("AWS_REGION", "").strip() or None),
                cache_enabled=cfg.cache_enabled,
                cache_dir=cfg.cache_dir,
                strategy=cfg.cache_strategy,
            )
        return FileSystemSource(base_path=cfg.filesystem_base_path or "")

    def _read_bytes(self, context: PipelineContext, source, ref: str) -> bytes:
        if ref.startswith("filesystem://"):
            with open(ref[len("filesystem://") :], "rb") as fh:
                return fh.read()
        if ref.startswith("fs://"):
            with open(ref[len("fs://") :], "rb") as fh:
                return fh.read()
        if ref.startswith("s3://"):
            return source.read(ref[len("s3://") :])
        return context.artifacts.read(ref)

    def run(
        self, context: PipelineContext, inputs: PrepParseWeatherDataInputs
    ) -> PrepWeatherParseResult:
        # region_type and date_range flow down from top-level data config
        data_raw = context.config.get("data") or {}
        top_region_type = str(data_raw.get("region_type", "")).strip()
        date_range = data_raw.get("date_range") or {}

        weather_parse_raw = dict(_section(context.config, "data.weather_parse"))
        if top_region_type and not weather_parse_raw.get("region_type"):
            weather_parse_raw["region_type"] = top_region_type

        cfg = PrepWeatherParseConfig.from_raw(weather_parse_raw)
        out_cfg = PrepOutputConfig.from_raw(
            _section(context.config, "data.prepared_data")
        )
        dl_cfg = PrepWeatherDownloadConfig.from_raw(
            _section(context.config, "data.weather_download")
        )

        source = self._build_weather_source(dl_cfg)
        files = inputs.download_weather_data.downloaded_files

        if not files:
            context.log.warning(
                "prep parse_weather_data: no downloaded files, skipping"
            )
            dest = Path(out_cfg.base_dir) / cfg.region_type / "weather_daily.csv"
            return PrepWeatherParseResult(
                region_type=cfg.region_type,
                prepared_data_path=str(dest.resolve()),
                total_rows=0,
            )

        raw_dfs = [
            pd.read_csv(
                io.BytesIO(self._read_bytes(context, source, f)), low_memory=False
            )
            for f in files
        ]

        merged = pd.concat(raw_dfs, ignore_index=True)
        context.log.info(
            "prep parse_weather_data: loaded %d CSV file(s), concatenated %d rows total",
            len(raw_dfs),
            len(merged),
        )

        merged = weather.normalise_columns(merged)

        if "region_id" not in merged.columns:
            for candidate in [
                "location.admin3.ID",
                "location.admin2.ID",
                "location.admin4.ID",
            ]:
                if candidate in merged.columns:
                    merged.rename(columns={candidate: "region_id"}, inplace=True)
                    context.log.debug(
                        "prep parse_weather_data: mapped region_id from column %r",
                        candidate,
                    )
                    break
            else:
                context.log.warning(
                    "prep parse_weather_data: no region_id column found; checked "
                    "['location.admin3.ID', 'location.admin2.ID', 'location.admin4.ID'] — "
                    "downstream aggregation will fail"
                )

        if "date" not in merged.columns and "metadata.primaryDate" in merged.columns:
            merged.rename(columns={"metadata.primaryDate": "date"}, inplace=True)
            context.log.debug(
                "prep parse_weather_data: remapped 'metadata.primaryDate' → 'date'"
            )

        daily = weather.aggregate_daily(
            merged,
            cfg.weather_variables,
            daily_agg=cfg.daily_agg,
        )
        context.log.info(
            "prep parse_weather_data: daily aggregation → %d (region_id, date) rows; "
            "variables=%s",
            len(daily),
            cfg.weather_variables,
        )

        # Apply date range filter — only if set in config
        date_start = (
            pd.Timestamp(date_range["start"]).normalize()
            if date_range.get("start")
            else None
        )
        date_end = (
            pd.Timestamp(date_range["end"]).normalize()
            if date_range.get("end")
            else None
        )

        daily["date"] = pd.to_datetime(daily["date"])
        n_before_filter = len(daily)
        if date_start is not None:
            daily = daily[daily["date"] >= date_start]
        if date_end is not None:
            daily = daily[daily["date"] <= date_end]
        if date_start is not None or date_end is not None:
            context.log.info(
                "prep parse_weather_data: date filter [%s → %s]: "
                "%d rows → %d rows (dropped %d)",
                date_start.date() if date_start else "unbounded",
                date_end.date() if date_end else "unbounded",
                n_before_filter,
                len(daily),
                n_before_filter - len(daily),
            )

        dest = Path(out_cfg.base_dir) / cfg.region_type / "weather_daily.csv"
        total = _write_csv(dest, daily, key_cols=["region_id", "date"])
        context.log.info(
            "prep parse_weather_data: region_type=%s wrote %d rows → %s",
            cfg.region_type,
            len(daily),
            dest,
        )
        return PrepWeatherParseResult(
            region_type=cfg.region_type,
            prepared_data_path=str(dest.resolve()),
            total_rows=total,
        )
